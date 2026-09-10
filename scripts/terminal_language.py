"""Frontend terminal locale projection; never writes a language preference."""
import locale
import os

TEXT = {
 'One HASHI program, with isolated named instances.':'一个 HASHI 程序，管理相互独立的命名实例。',
 'Start the selected instance in the background.':'在后台启动所选实例。',
 'Open the terminal UI for the selected instance.':'打开所选实例的终端界面。',
 'Show selected instance and live process state.':'查看所选实例及实际运行状态。',
 'Gracefully stop an idle selected instance.':'在所选实例空闲时正常停止。',
 'Open local connection or Hashiko assistance.':'打开本地接通页或小乔协助。',
 'Open an installed external HASHI UI.':'打开已安装的外部 HASHI 界面。',
 'Show this help.':'显示帮助。',
 'Manage isolated instances.':'管理独立实例。',
 'Select an exact registered instance name.':'选择已登记的确切实例名称。',
 'Emit machine-readable output.':'输出机器可读的 JSON。',
 'Show installed and selected runtime versions.':'查看已安装及所选运行版本。',
 'Inspect installation and local health without changing it.':'只读检查安装和本地健康状态。',
 'Read sanitized instance runtime logs.':'读取已脱敏的实例运行日志。',
 'Print shell completion; profiles are not changed.':'输出 Shell 补全脚本，不修改用户配置。',
 'Create or register an instance.':'创建或登记实例。',
 'List registered instances.':'列出已登记实例。',
 'Set the default instance.':'查看或设置默认实例。',
 'Bind a directory to an instance.':'将目录绑定到实例。',
 'Remove a directory binding without deleting files.':'解除目录绑定，不删除文件。',
 'Unregister and retain recoverable data by default.':'解除登记，默认保留可恢复数据。',
 'Restore the newest recoverable removal.':'恢复最近移除时保留的数据。',
 'Explicitly adopt the installed program version.':'明确采用已安装的程序版本。',
 'Register an existing HASHI Git/program root without changing it.':'登记已有 HASHI Git／程序目录，保留原内容。',
 'Make it the default.':'设为默认实例。',
 'Bind this directory (and descendants) to the instance.':'将此目录及其子目录绑定到实例。',
 'Existing bridge home (requires --from; defaults to that code root).':'已有实例数据目录（须配合 --from，默认同代码目录）。',
 'show this help message and exit':'显示帮助并退出',
 'positional arguments:':'位置参数：','options:':'选项：','usage:':'用法：',
 'No HASHI instances are registered.':'尚未登记 HASHI 实例。',
 'Conflicting instance targets':'指定的实例目标互相冲突',
 '--all and --instance cannot be combined':'--all 与 --instance 不能同时使用',
 'timeout must be an integer from 1 to 300':'timeout 必须为 1 至 300 的整数',
 'lines must be an integer from 1 to 10000':'lines 必须为 1 至 10000 的整数',
 'Instance: ':'实例：','Selected by: ':'选择依据：','State: ':'状态：','Busy: ':'忙碌：',
 'Next: hashi help; hashi status; hashi doctor':'下一步：hashi help；hashi status；hashi doctor',
 'Start was sent; readiness is unconfirmed. Process was not killed.':'已发送启动请求，就绪状态尚未确认，未终止进程。',
 'Shutdown was accepted but the instance is still running; no force kill was attempted.':'已接受停止请求，但实例仍在运行，未强制终止。',
 'No runtime log exists.':'尚无运行日志。',
}


def resolve(argv):
    value='auto'
    for index,token in enumerate(argv):
        if token=='--lang' and index+1<len(argv):value=argv[index+1]
        elif token.startswith('--lang='):value=token.split('=',1)[1]
    if value=='auto':
        value=os.environ.get('HASHI_UI_LANGUAGE') or locale.getlocale()[0] or 'en'
    return 'zh' if value.lower().startswith('zh') else 'en'


def localize(text, language):
    if language=='zh':
        for source,target in sorted(TEXT.items(),key=lambda item:len(item[0]),reverse=True):
            text=text.replace(source,target)
    return text
