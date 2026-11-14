// In-process, hidden native controls only. Never activates a window or sends OS input.
using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Web.Script.Serialization;
class NativeDesktopTextRegression {
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]static extern IntPtr LoadLibraryEx(string path,IntPtr file,uint flags);
 [DllImport("user32.dll",CharSet=CharSet.Unicode,SetLastError=true)]static extern IntPtr CreateWindowEx(uint ex,string cls,string title,uint style,int x,int y,int w,int h,IntPtr parent,IntPtr menu,IntPtr instance,IntPtr data);
 [DllImport("user32.dll")]static extern bool DestroyWindow(IntPtr h);
 [DllImport("user32.dll")]static extern bool IsWindowVisible(IntPtr h);
 [DllImport("user32.dll",CharSet=CharSet.Unicode)]static extern IntPtr SendMessage(IntPtr h,uint m,IntPtr w,string s);
 [DllImport("user32.dll",EntryPoint="SendMessageW")]static extern IntPtr SendNumber(IntPtr h,uint m,IntPtr w,IntPtr l);
 [DllImport("user32.dll",CharSet=CharSet.Unicode)]static extern int GetWindowText(IntPtr h,StringBuilder s,int n);
 static string Read(IntPtr h){var s=new StringBuilder(12000);GetWindowText(h,s,s.Capacity);return s.ToString().Replace("\r\n","\n").Replace("\r","\n");}
 static void Require(bool ok,string message){if(!ok)throw new Exception(message);}
 [STAThread]static int Main(string[] args){
  var observations=new List<object>();IntPtr edit=IntPtr.Zero;
  try{
   var assembly=Assembly.LoadFrom(args[0]);var host=assembly.GetType("HashiDesktop.Host+Desktop",true);var api=assembly.GetType("HashiDesktop.Host+N",true);
   var constructor=host.GetConstructor(BindingFlags.Instance|BindingFlags.NonPublic,null,new[]{typeof(Func<string,bool>)},null);
   Require(constructor!=null,"native text dispatch has no single-operation writer");
   var replace=api.GetMethod("ReplaceRichEditText",BindingFlags.Static|BindingFlags.NonPublic);
   Require(replace!=null,"modern RichEdit selection writer missing");
   Require(LoadLibraryEx(args[1],IntPtr.Zero,8)!=IntPtr.Zero,"native RichEdit test module unavailable");
   edit=CreateWindowEx(0,"RichEditD2DPT","HASHI hidden regression",0x80001004,0,0,600,100,IntPtr.Zero,IntPtr.Zero,IntPtr.Zero,IntPtr.Zero);
   Require(edit!=IntPtr.Zero,"hidden native control unavailable");
   Require(!IsWindowVisible(edit),"native test control unexpectedly visible");
   foreach(string text in new[]{"HASHI desktop recovery verified 2026-11-09","中文组合🌸 mixed é","first\r\nsecond\t第三行",new string('界',4096),""}){
    SendMessage(edit,12,IntPtr.Zero,"prefix OLD suffix");
    SendNumber(edit,0xb1,new IntPtr(7),new IntPtr(10));
    SendNumber(edit,0xcd,IntPtr.Zero,IntPtr.Zero);
    int calls=0;
    var native=constructor.Invoke(new object[]{new Func<string,bool>(delegate(string value){calls++;replace.Invoke(null,new object[]{edit,value});return true;})});
    host.GetMethod("Run").Invoke(native,new object[]{"input",new Dictionary<string,object>{{"kind","text"},{"text",text}}});
    string expected=text.Length==0 ? "prefix OLD suffix" : "prefix "+text.Replace("\r\n","\n").Replace("\r","\n")+" suffix";
    Require(Read(edit)==expected,"selected native content differs from submitted Unicode");
    Require(calls==(text.Length==0 ? 0 : 1),"native text was split, replayed or empty text changed the selection");
    if(text.Length>0){SendNumber(edit,0x304,IntPtr.Zero,IntPtr.Zero);Require(Read(edit)=="prefix OLD suffix","one undo did not restore original selection");}
    observations.Add(new{length=text.Length,selection_preserved=true,one_operation=true,undo_checked=text.Length>0});
   }
   int attempted=0;
   var failing=constructor.Invoke(new object[]{new Func<string,bool>(delegate(string value){attempted++;throw new TimeoutException("uncertain native edit");})});
   bool rejected=false;
   try{host.GetMethod("Run").Invoke(failing,new object[]{"input",new Dictionary<string,object>{{"kind","text"},{"text","uncertain"}}});}
   catch(TargetInvocationException e){rejected=e.InnerException is TimeoutException;}
   Require(rejected && attempted==1,"uncertain edit was replayed or fell back to OS input");
   int oversize=0;
   var bounded=constructor.Invoke(new object[]{new Func<string,bool>(delegate(string value){oversize++;return true;})});
   rejected=false;
   try{host.GetMethod("Run").Invoke(bounded,new object[]{"input",new Dictionary<string,object>{{"kind","text"},{"text",new string('x',4097)}}});}
   catch(TargetInvocationException e){rejected=e.InnerException is InvalidOperationException;}
   Require(rejected && oversize==0,"oversized payload reached native edit");
   Console.WriteLine(new JavaScriptSerializer().Serialize(new{ok=true,observations=observations,timeout_not_replayed=true,oversize_rejected=true,visible_windows=IsWindowVisible(edit) ? 1 : 0}));return 0;
  }catch(Exception e){Console.WriteLine(new JavaScriptSerializer().Serialize(new{ok=false,error=e.ToString(),visible_windows=0}));return 1;}
  finally{if(edit!=IntPtr.Zero)DestroyWindow(edit);}
 }
}
