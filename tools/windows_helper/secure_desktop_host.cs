// Local, session-bound Windows desktop adapter. No sockets, commands or credentials.
// Installed executable and configuration must be writable only by SYSTEM/Admins.
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.IO.Pipes;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.ServiceProcess;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;

namespace HashiDesktop {
  sealed class Host : ServiceBase {
    readonly string configPath;
    readonly Dictionary<string, object> config;
    volatile bool stopping;
    Host(string path) { configPath=path; config=ReadConfig(path); ServiceName=S(config,"service_name"); }
    static Dictionary<string,object> ReadConfig(string path) {
      return new JavaScriptSerializer().Deserialize<Dictionary<string,object>>(File.ReadAllText(path));
    }
    static string S(Dictionary<string,object> d,string k) { return Convert.ToString(d[k]); }
    static int I(Dictionary<string,object> d,string k) { return Convert.ToInt32(d[k]); }
    static int Main(string[] args) {
      if (!WindowsIdentity.GetCurrent().IsSystem || args.Length != 2) return 3;
      try {
        if(args[0]=="--agent") { Agent(ReadConfig(args[1])); return 0; }
        if(args[0]=="--service") { ServiceBase.Run(new Host(args[1])); return 0; }
      } catch(Exception e) { SafeLog(args[1],"host-exited",e); return 2; }
      return 3;
    }
    protected override void OnStart(string[] args) {
      new Thread(Supervise) { IsBackground=true, Name="desktop-session-supervisor" }.Start();
    }
    protected override void OnStop() {
      stopping=true;
      try {
        using(var pipe=new NamedPipeClientStream(".",S(config,"pipe_name"),PipeDirection.Out)) {
          pipe.Connect(1000);
          byte[] body=Encoding.UTF8.GetBytes("{\"operation\":\"shutdown\",\"args\":{}}");
          byte[] prefix=BitConverter.GetBytes(body.Length);
          pipe.Write(prefix,0,4);pipe.Write(body,0,body.Length);pipe.Flush();
        }
      } catch {}
    }
    void Supervise() {
      Process child=null;
      while(!stopping) {
        try {
          if(child==null || child.HasExited) {
            if(child!=null) child.Dispose();
            int session=I(config,"session_id");
            // This installation only controls the verified console user/session.
            if(N.ConsoleSession()!=session || N.SessionSid(session)!=S(config,"user_sid")) {
              Thread.Sleep(1000); continue;
            }
            child=N.SpawnSession(session,configPath);
          }
        } catch(Exception e) { SafeLog(configPath,"session-start-failed",e); Thread.Sleep(1000); }
        Thread.Sleep(500);
      }
    }
    static void Agent(Dictionary<string,object> config) {
      int session=I(config,"session_id");
      if(Process.GetCurrentProcess().SessionId!=session || N.SessionSid(session)!=S(config,"user_sid")) return;
      N.SetProcessDpiAwarenessContext(new IntPtr(-4));
      var acl=new PipeSecurity();
      acl.SetAccessRuleProtection(true,false);
      acl.SetOwner(new SecurityIdentifier("S-1-5-18"));
      acl.AddAccessRule(new PipeAccessRule(new SecurityIdentifier("S-1-5-18"),PipeAccessRights.FullControl,AccessControlType.Allow));
      acl.AddAccessRule(new PipeAccessRule(new SecurityIdentifier(S(config,"user_sid")),PipeAccessRights.ReadWrite,AccessControlType.Allow));
      var native=new Desktop();
      var json=new JavaScriptSerializer { MaxJsonLength=8000000, RecursionLimit=16 };
      var guard=new object();
      using(var watchdog=new Timer(delegate(object unused) {
        lock(guard) {
          try { if(native.NeedsReset()) { using(new InputDesktop()) native.Reset(); } } catch {}
        }
      },null,500,500)) {
      while(N.ConsoleSession()==session && N.SessionSid(session)==S(config,"user_sid")) {
        using(var pipe=new NamedPipeServerStream(S(config,"pipe_name"),PipeDirection.InOut,1,PipeTransmissionMode.Byte,
          PipeOptions.Asynchronous,65536,65536,acl)) {
          pipe.WaitForConnection();
          try {
            uint client;
            if(N.ConsoleSession()!=session || N.SessionSid(session)!=S(config,"user_sid") ||
               !N.GetNamedPipeClientProcessId(pipe.SafePipeHandle.DangerousGetHandle(),out client)) throw new InvalidOperationException();
            int length=BitConverter.ToInt32(Read(pipe,4),0);
            if(length<=0 || length>32768) throw new InvalidOperationException();
            byte[] received=Read(pipe,length);
            // Named-pipe impersonation is valid only AFTER the first client
            // write. Reading a bounded envelope does not authorize any action.
            string caller="";
            pipe.RunAsClient(delegate { caller=WindowsIdentity.GetCurrent().User.Value; });
            if(caller!=S(config,"user_sid") && caller!="S-1-5-18") throw new InvalidOperationException();
            var body=json.Deserialize<Dictionary<string,object>>(Encoding.UTF8.GetString(received));
            if(body.Count!=2 || !body.ContainsKey("operation") || !body.ContainsKey("args")) throw new InvalidOperationException();
            var a=body["args"] as Dictionary<string,object>;
            if(a==null) throw new InvalidOperationException();
            int clientSession=Process.GetProcessById((int)client).SessionId;
            if(S(body,"operation")=="shutdown" && caller=="S-1-5-18" && clientSession==0) return;
            if(clientSession!=session) throw new InvalidOperationException();
            object result;
            lock(guard) { using(new InputDesktop()) { result=native.Run(S(body,"operation"),a); } }
            Write(pipe,json.Serialize(new { ok=true, result=result }));
          } catch(Exception e) {
            SafeLog(S(config,"host_executable"),"desktop-request-failed",e);
            try { Write(pipe,json.Serialize(new { ok=false, error_code="desktop_secure_host_unavailable" })); } catch {}
          }
        }
      }
      }
    }
    static void SafeLog(string path,string code,Exception e) {
      try {
        File.AppendAllText(Path.Combine(Path.GetDirectoryName(path),"host-events.jsonl"),
          new JavaScriptSerializer().Serialize(new { at=DateTime.UtcNow.ToString("o"),code=code,
            error_type=e.GetType().Name,os_error=e is Win32Exception ? ((Win32Exception)e).NativeErrorCode : 0 })+"\n");
      } catch {}
    }
    static byte[] Read(Stream stream,int count) {
      var b=new byte[count]; int offset=0;
      while(offset<count) {
        var pending=stream.BeginRead(b,offset,count-offset,null,null);
        int n;
        using(pending.AsyncWaitHandle) {
          if(!pending.AsyncWaitHandle.WaitOne(3500)) throw new IOException();
          n=stream.EndRead(pending);
        }
        if(n<=0) throw new IOException(); offset+=n;
      }
      return b;
    }
    static void Write(Stream stream,string value) {
      byte[] b=Encoding.UTF8.GetBytes(value);
      if(b.Length>8000000) throw new IOException();
      var prefix=BitConverter.GetBytes(b.Length);
      stream.Write(prefix,0,4);
      var pending=stream.BeginWrite(b,0,b.Length,null,null);
      using(pending.AsyncWaitHandle) {
        if(!pending.AsyncWaitHandle.WaitOne(3500)) throw new IOException();
        stream.EndWrite(pending);
      }
      stream.Flush();
    }
    sealed class InputDesktop : IDisposable {
      readonly IntPtr before, current;
      public InputDesktop() {
        before=N.GetThreadDesktop(N.GetCurrentThreadId());
        current=N.OpenInputDesktop(0,false,0x01ff);
        if(current==IntPtr.Zero) throw new Win32Exception();
        if(!N.SetThreadDesktop(current)) { N.CloseDesktop(current); throw new Win32Exception(); }
      }
      public void Dispose() {
        // GDI objects are disposed before leaving this scope. Never SwitchDesktop.
        N.SetThreadDesktop(before); N.CloseDesktop(current);
      }
    }
    sealed class Desktop {
      readonly HashSet<int> keys=new HashSet<int>();
      readonly HashSet<int> buttons=new HashSet<int>();
      DateTime lastInput=DateTime.UtcNow;
      public bool NeedsReset() { return (keys.Count>0 || buttons.Count>0) && (DateTime.UtcNow-lastInput).TotalSeconds>=3; }
      public object Run(string op,Dictionary<string,object> a) {
        if(op=="status") {
          if(a.Count!=0) throw new InvalidOperationException();
          return new { available=true, interactive=true, locked=N.Locked(), secure_desktop=N.Secure(), session_id=Process.GetCurrentProcess().SessionId };
        }
        if(op=="cursor") {
          if(a.Count!=0) throw new InvalidOperationException();
          N.POINT p; if(!N.GetCursorPos(out p)) throw new Win32Exception();
          return new { x=p.x, y=p.y, visible=true };
        }
        if(op=="capture") {
          if(a.Count!=6) throw new InvalidOperationException();
          int x=I(a,"x"), y=I(a,"y"), w=I(a,"width"), h=I(a,"height"), ow=I(a,"output_width"), oh=I(a,"output_height");
          if(w<=0 || h<=0 || w>16000 || h>16000 || ow<=0 || oh<=0 || ow>1600 || oh>900) throw new InvalidOperationException();
          var bounds=new Rectangle(N.GetSystemMetrics(76),N.GetSystemMetrics(77),N.GetSystemMetrics(78),N.GetSystemMetrics(79));
          if(!bounds.Contains(new Rectangle(x,y,w,h))) throw new InvalidOperationException();
          IntPtr screen=N.GetDC(IntPtr.Zero), dest=IntPtr.Zero;
          try {
            using(var bmp=new Bitmap(ow,oh,PixelFormat.Format24bppRgb)) {
              using(var g=Graphics.FromImage(bmp)) {
                try {
                  dest=g.GetHdc(); N.SetStretchBltMode(dest,4);
                  if(!N.StretchBlt(dest,0,0,ow,oh,screen,x,y,w,h,0x40cc0020)) throw new Win32Exception();
                } finally { if(dest!=IntPtr.Zero) { g.ReleaseHdc(dest); dest=IntPtr.Zero; } }
              }
              using(var output=new MemoryStream()) {
                bmp.Save(output,ImageFormat.Png);
                if(output.Length>5500000) throw new InvalidOperationException();
                return new { image=Convert.ToBase64String(output.ToArray()) };
              }
            }
          } finally { if(screen!=IntPtr.Zero) N.ReleaseDC(IntPtr.Zero,screen); }
        }
        if(op=="reset") { if(a.Count!=0) throw new InvalidOperationException(); Reset(); return new { injected=true }; }
        if(op!="input") throw new InvalidOperationException();
        lastInput=DateTime.UtcNow;
        string kind=S(a,"kind");
        if(kind=="move" || kind=="down" || kind=="wheel") {
          int x=I(a,"px"),y=I(a,"py");
          var bounds=new Rectangle(N.GetSystemMetrics(76),N.GetSystemMetrics(77),N.GetSystemMetrics(78),N.GetSystemMetrics(79));
          if(!bounds.Contains(x,y)) throw new InvalidOperationException();
          if(!N.SetCursorPos(x,y)) throw new Win32Exception();
          N.POINT p; if(!N.GetCursorPos(out p) || p.x!=x || p.y!=y) throw new Win32Exception();
        }
        if(kind=="down" || kind=="up") {
          int button=S(a,"button")=="left" ? 0 : S(a,"button")=="right" ? 1 : S(a,"button")=="middle" ? 2 : -1;
          if(button<0) throw new InvalidOperationException();
          if(kind=="down") { Mouse(new uint[]{2,8,32}[button],0); buttons.Add(button); }
          else if(buttons.Contains(button)) { Mouse(new uint[]{4,16,64}[button],0); buttons.Remove(button); }
        } else if(kind=="wheel") {
          int delta=I(a,"delta"); if(delta < -1200 || delta>1200) throw new InvalidOperationException();
          Mouse(a.ContainsKey("horizontal") && Convert.ToBoolean(a["horizontal"]) ? 0x1000u : 0x800u,unchecked((uint)delta));
        } else if(kind=="key_down" || kind=="key_up") {
          int vk=I(a,"vk"); uint flags=(a.ContainsKey("extended") && Convert.ToBoolean(a["extended"])) ? 1u : 0u;
          if(vk<=0 || vk>255) throw new InvalidOperationException();
          // Secure attention is owned by Windows; this bridge never synthesizes it.
          if(kind=="key_down") { Key(vk,0,flags); keys.Add(vk); }
          else if(keys.Contains(vk)) { Key(vk,0,flags|2); keys.Remove(vk); }
        } else if(kind=="text") {
          string text=S(a,"text"); if(text.Length>4096) throw new InvalidOperationException();
          foreach(char c in text.Replace("\r\n","\n").Replace("\r","\n")) {
            if(c=='\n' || c=='\t') { int vk=c=='\n' ? 13 : 9; Key(vk,0,0); Key(vk,0,2); }
            else { TextCharacter(c); }
          }
        } else if(kind!="move") throw new InvalidOperationException();
        return new { injected=true };
      }
      public void Reset() {
        foreach(int k in keys) Key(k,0,2u | (k==163 || k==165 || k>=33 && k<=46 || k==91 || k==92 ? 1u : 0u)); keys.Clear();
        foreach(int b in buttons) Mouse(new uint[]{4,16,64}[b],0); buttons.Clear();
      }
      static void Key(int vk,int scan,uint flags) {
        var i=new N.INPUT { type=1 }; i.value.key=new N.KEY { vk=(ushort)vk, scan=(ushort)scan, flags=flags };
        N.Send(i);
      }
      static void TextCharacter(char c) {
        // Windows PIN providers reject VK_PACKET even when SendInput reports
        // success. On the login desktop use its active keyboard layout for
        // representable characters, retaining Unicode for unmapped characters.
        if(N.Secure()) {
          uint process; uint thread=N.GetWindowThreadProcessId(N.GetForegroundWindow(),out process);
          short mapping=N.VkKeyScanEx(c,N.GetKeyboardLayout(thread));
          if(mapping!=-1 && (mapping & 0xf800)==0) {
            int modifiers=(mapping>>8)&7;
            var pressed=new List<int>();
            try {
              int[] codes={16,17,18};
              for(int bit=0;bit<3;bit++) {
                if((modifiers & (1<<bit))!=0 && (N.GetAsyncKeyState(codes[bit]) & 0x8000)==0) {
                  Key(codes[bit],0,0); pressed.Add(codes[bit]);
                }
              }
              Key(mapping & 255,0,0); Key(mapping & 255,0,2);
              return;
            } finally { for(int i=pressed.Count-1;i>=0;i--) Key(pressed[i],0,2); }
          }
        }
        Key(0,c,4); Key(0,c,6);
      }
      static void Mouse(uint flags,uint data) {
        var i=new N.INPUT { type=0 }; i.value.mouse=new N.MOUSE { flags=flags, data=data }; N.Send(i);
      }
    }
    static class N {
      [StructLayout(LayoutKind.Sequential)] internal struct POINT { public int x,y; }
      [StructLayout(LayoutKind.Sequential)] internal struct MOUSE { public int x,y; public uint data,flags,time; public UIntPtr extra; }
      [StructLayout(LayoutKind.Sequential)] internal struct KEY { public ushort vk,scan; public uint flags,time; public UIntPtr extra; }
      [StructLayout(LayoutKind.Explicit)] internal struct UNION { [FieldOffset(0)] public MOUSE mouse; [FieldOffset(0)] public KEY key; }
      [StructLayout(LayoutKind.Sequential)] internal struct INPUT { public uint type; public UNION value; }
      [StructLayout(LayoutKind.Sequential,CharSet=CharSet.Unicode)] struct STARTUP { public int cb; public string reserved,desktop,title; public int x,y,w,h,cx,cy,fill,flags; public short show,reserved2; public IntPtr reservedPtr,input,output,error; }
      [StructLayout(LayoutKind.Sequential)] struct PROCESS { public IntPtr process,thread; public uint pid,tid; }
      [StructLayout(LayoutKind.Sequential)] struct LUID { public uint low; public int high; }
      [StructLayout(LayoutKind.Sequential)] struct PRIVILEGES { public int count; public LUID luid; public int attributes; }
      [DllImport("user32.dll",SetLastError=true)] internal static extern IntPtr OpenInputDesktop(uint f,bool inherit,uint access);
      [DllImport("user32.dll",SetLastError=true)] internal static extern bool SetThreadDesktop(IntPtr desktop);
      [DllImport("user32.dll")] internal static extern IntPtr GetThreadDesktop(uint thread);
      [DllImport("user32.dll")] internal static extern bool CloseDesktop(IntPtr desktop);
      [DllImport("user32.dll")] internal static extern bool SetProcessDpiAwarenessContext(IntPtr value);
      [DllImport("kernel32.dll")] internal static extern uint GetCurrentThreadId();
      [DllImport("kernel32.dll")] internal static extern int WTSGetActiveConsoleSessionId();
      [DllImport("kernel32.dll",SetLastError=true)] internal static extern bool GetNamedPipeClientProcessId(IntPtr pipe,out uint pid);
      [DllImport("user32.dll",SetLastError=true)] internal static extern bool SetCursorPos(int x,int y);
      [DllImport("user32.dll",SetLastError=true)] internal static extern bool GetCursorPos(out POINT point);
      [DllImport("user32.dll")] internal static extern IntPtr GetForegroundWindow();
      [DllImport("user32.dll")] internal static extern uint GetWindowThreadProcessId(IntPtr window,out uint process);
      [DllImport("user32.dll")] internal static extern IntPtr GetKeyboardLayout(uint thread);
      [DllImport("user32.dll",CharSet=CharSet.Unicode,EntryPoint="VkKeyScanExW")] internal static extern short VkKeyScanEx(char value,IntPtr layout);
      [DllImport("user32.dll")] internal static extern short GetAsyncKeyState(int key);
      [DllImport("user32.dll")] internal static extern int GetSystemMetrics(int i);
      [DllImport("user32.dll")] internal static extern IntPtr GetDC(IntPtr window);
      [DllImport("user32.dll")] internal static extern int ReleaseDC(IntPtr window,IntPtr dc);
      [DllImport("gdi32.dll")] internal static extern int SetStretchBltMode(IntPtr dc,int mode);
      [DllImport("gdi32.dll",SetLastError=true)] internal static extern bool StretchBlt(IntPtr d,int x,int y,int w,int h,IntPtr s,int sx,int sy,int sw,int sh,uint rop);
      [DllImport("user32.dll",SetLastError=true)] static extern uint SendInput(uint count,INPUT[] inputs,int size);
      [DllImport("user32.dll",CharSet=CharSet.Unicode)] static extern bool GetUserObjectInformation(IntPtr h,int n,StringBuilder b,int length,out int needed);
      [DllImport("wtsapi32.dll",SetLastError=true)] static extern bool WTSQueryUserToken(int session,out IntPtr token);
      [DllImport("wtsapi32.dll")] static extern bool WTSQuerySessionInformation(IntPtr server,int session,int info,out IntPtr buffer,out int bytes);
      [DllImport("wtsapi32.dll")] static extern void WTSFreeMemory(IntPtr b);
      [DllImport("advapi32.dll",SetLastError=true)] static extern bool OpenProcessToken(IntPtr process,uint access,out IntPtr token);
      [DllImport("advapi32.dll",SetLastError=true)] static extern bool DuplicateTokenEx(IntPtr token,uint access,IntPtr attr,int level,int type,out IntPtr copy);
      [DllImport("advapi32.dll",SetLastError=true)] static extern bool SetTokenInformation(IntPtr token,int info,ref int value,int length);
      [DllImport("advapi32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern bool LookupPrivilegeValue(string system,string name,out LUID luid);
      [DllImport("advapi32.dll",SetLastError=true)] static extern bool AdjustTokenPrivileges(IntPtr token,bool disable,ref PRIVILEGES state,int length,IntPtr before,IntPtr size);
      [DllImport("advapi32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern bool CreateProcessAsUser(IntPtr token,string app,StringBuilder command,IntPtr pa,IntPtr ta,bool inherit,uint flags,IntPtr env,string cwd,ref STARTUP startup,out PROCESS process);
      [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
      internal static int ConsoleSession() { return WTSGetActiveConsoleSessionId(); }
      internal static string SessionSid(int session) {
        IntPtr token; if(!WTSQueryUserToken(session,out token)) throw new Win32Exception();
        try { using(var id=new WindowsIdentity(token)) return id.User.Value; } finally { CloseHandle(token); }
      }
      internal static void Send(INPUT i) { if(SendInput(1,new[]{i},Marshal.SizeOf(typeof(INPUT)))!=1) throw new Win32Exception(); }
      internal static bool Secure() {
        var name=new StringBuilder(256); int length;
        if(!GetUserObjectInformation(GetThreadDesktop(GetCurrentThreadId()),2,name,512,out length)) throw new Win32Exception();
        return !String.Equals(name.ToString(),"Default",StringComparison.OrdinalIgnoreCase);
      }
      internal static bool Locked() {
        IntPtr b; int bytes;
        if(!WTSQuerySessionInformation(IntPtr.Zero,Process.GetCurrentProcess().SessionId,25,out b,out bytes)) return Secure();
        try { return Secure() || (bytes>=20 && Marshal.ReadInt32(b,0)==1 && Marshal.ReadInt32(b,16)==0); }
        finally { WTSFreeMemory(b); }
      }
      internal static Process SpawnSession(int session,string configPath) {
        IntPtr token=IntPtr.Zero,copy=IntPtr.Zero;
        try {
          if(!OpenProcessToken(Process.GetCurrentProcess().Handle,0xF01FF,out token)) throw new Win32Exception();
          foreach(string name in new[]{"SeTcbPrivilege","SeAssignPrimaryTokenPrivilege","SeIncreaseQuotaPrivilege"}) {
            var state=new PRIVILEGES { count=1,attributes=2 };
            if(!LookupPrivilegeValue(null,name,out state.luid) || !AdjustTokenPrivileges(token,false,ref state,0,IntPtr.Zero,IntPtr.Zero)) throw new Win32Exception();
          }
          if(!DuplicateTokenEx(token,0xF01FF,IntPtr.Zero,2,1,out copy) || !SetTokenInformation(copy,12,ref session,4)) throw new Win32Exception();
          string exe=System.Reflection.Assembly.GetExecutingAssembly().Location;
          var startup=new STARTUP { cb=Marshal.SizeOf(typeof(STARTUP)),desktop="winsta0\\default" };
          PROCESS p;
          if(!CreateProcessAsUser(copy,exe,new StringBuilder("\""+exe+"\" --agent \""+configPath+"\""),IntPtr.Zero,IntPtr.Zero,false,0x08000000,IntPtr.Zero,Path.GetDirectoryName(exe),ref startup,out p)) throw new Win32Exception();
          CloseHandle(p.thread); CloseHandle(p.process); return Process.GetProcessById((int)p.pid);
        } finally { if(copy!=IntPtr.Zero) CloseHandle(copy); if(token!=IntPtr.Zero) CloseHandle(token); }
      }
    }
  }
}
