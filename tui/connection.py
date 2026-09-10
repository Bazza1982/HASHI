"""Model-independent local connection page. Secret inputs never enter chat."""
from __future__ import annotations

from pathlib import Path
from tools.terminal_environment import has_interactive_input
from textual import work
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Select, Static

from onboarding.connection import ConnectionError, choices, validate, save, adopt_if_running, connect_telegram


class ConnectionScreen(ModalScreen[dict | None]):
    DEFAULT_CSS = '''
    ConnectionScreen { align: center middle; background: $background 85%; }
    #connection-page { width: 76; max-width: 96%; height: auto; max-height: 95%; padding: 1 2; background: $surface; border: solid $primary; }
    #connection-page Input, #connection-page Select { margin-bottom: 1; }
    #connection-buttons { height: auto; }
    #connection-buttons Button { margin-right: 1; }
    '''

    def __init__(self, home: Path, *, language='en'):
        super().__init__()
        self.home = Path(home)
        self.language = language
        self.options = choices(home)
        self.busy = False
        self.telegram_mode = False

    def t(self, en, zh):
        return zh if self.language == 'zh' else en

    def compose(self) -> ComposeResult:
        with VerticalScroll(id='connection-page'):
            yield Static(self.t('Connect Hashiko — Telegram is optional', '接通小乔 — Telegram 可跳过'))
            yield Static(self.t('Choose one backend. CLI discovery does not confirm login. No request is sent until you confirm.',
                                '选择一个后端。发现 CLI 不等于已登录。确认前不会发送验证请求。'))
            yield Select([(r['engine'] + (' · CLI' if r['kind']=='cli' else ' · API'),r['engine']) for r in self.options],prompt=self.t('Choose backend','选择后端'),id='connection-backend')
            yield Select([],prompt=self.t('Choose model','选择模型'),id='connection-model')
            yield Input(password=True,placeholder=self.t('API key (API only; kept out of chat)','API 密钥（仅 API 使用；不进入聊天）'),id='connection-key')
            yield Input(placeholder=self.t('Telegram numeric user ID (optional setup only)','Telegram 数字用户 ID（仅可选设置）'),id='connection-user-id')
            yield Checkbox(self.t('Allow one minimal model check (charges may apply) and reload idle Hashiko after saving.','允许一次最小模型验证（可能收费），保存后重载空闲的小乔。'),id='connection-consent')
            yield Checkbox(self.t('Replace the previously saved key for this provider, if different.','如该供应商已有不同密钥，确认替换。'),id='connection-replace')
            yield Static('',id='connection-result')
            with Horizontal(id='connection-buttons'):
                yield Button(self.t('Connect','连接'),id='connection-submit',variant='primary')
                yield Button(self.t('Cancel','取消'),id='connection-cancel')
                yield Button('Telegram',id='connection-telegram')
                yield Button('中文 / English',id='connection-language')

    def on_mount(self):
        self.query_one('#connection-user-id').display = False

    def on_input_changed(self, event: Input.Changed):
        event.stop()

    def on_input_submitted(self, event: Input.Submitted):
        event.stop()

    def on_select_changed(self,event: Select.Changed):
        event.stop()
        if event.select.id == 'connection-backend':
            selected = next((r for r in self.options if r['engine']==event.value),None)
            field = self.query_one('#connection-key',Input)
            field.value = ''
            field.disabled = not selected or selected['kind']=='cli'
            model = self.query_one('#connection-model',Select)
            model.set_options([(m,m) for m in selected['models']] if selected else [])
            if selected:
                model.value = selected['default'] if selected['default'] in selected['models'] else selected['models'][0]
            self.query_one('#connection-consent',Checkbox).value = False

    def on_button_pressed(self,event: Button.Pressed):
        event.stop()
        if event.button.id == 'connection-cancel':
            self.workers.cancel_node(self)
            self.query_one('#connection-key',Input).value = ''
            self.dismiss(None)
        elif event.button.id == 'connection-telegram' and not self.busy:
            self.telegram_mode = not self.telegram_mode
            self.query_one('#connection-user-id').display = self.telegram_mode
            for control in self.query(Select):
                control.display = not self.telegram_mode
            field = self.query_one('#connection-key',Input)
            field.value = ''
            field.disabled = False
            field.placeholder = 'Bot Token' if self.telegram_mode else self.t('API key','API 密钥')
            self.query_one('#connection-consent',Checkbox).value = False
            self.query_one('#connection-result',Static).update(self.t('Telegram is optional. Enter your numeric user ID, not a group ID.','Telegram 可跳过。请填您的数字用户 ID，不是群聊 ID。') if self.telegram_mode else '')
        elif event.button.id == 'connection-language' and not self.busy:
            self.language = 'en' if self.language=='zh' else 'zh'
            self.query_one('#connection-key',Input).value = ''
            self.recompose()
        elif event.button.id == 'connection-submit' and not self.busy:
            self.connect()

    @work(exclusive=True)
    async def connect(self):
        self.busy = True
        self.query_one('#connection-submit',Button).disabled = True
        key_input = self.query_one('#connection-key',Input)
        key = key_input.value
        for control in self.query(Select):
            control.disabled = True
        key_input.value = ''
        try:
            if self.telegram_mode:
                result = await connect_telegram(self.home,key,self.query_one('#connection-user-id',Input).value,
                    confirmed=self.query_one('#connection-consent',Checkbox).value,
                    replace_confirmed=self.query_one('#connection-replace',Checkbox).value)
                adoption = await adopt_if_running(self.home)
                self.dismiss({**result, **adoption})
                return
            engine = self.query_one('#connection-backend',Select).value
            model = self.query_one('#connection-model',Select).value
            if not isinstance(engine,str) or not isinstance(model,str):
                raise ConnectionError('SELECTION_REQUIRED')
            proof = await validate(self.home,engine,model,key,
                confirmed=self.query_one('#connection-consent',Checkbox).value)
            result = save(self.home,engine,model,key,verified=proof,
                replace_confirmed=self.query_one('#connection-replace',Checkbox).value,language=self.language)
            adoption = await adopt_if_running(self.home)
            self.dismiss({**result, **adoption})
        except ConnectionError as exc:
            self.query_one('#connection-result',Static).update(exc.code + '\n' + self.t('Retry, choose another backend, or cancel.','可重试、换后端或取消。'))
        except Exception:
            # Provider exceptions may contain request headers; never display raw errors.
            self.query_one('#connection-result',Static).update('CONNECTION_FAILED')
        finally:
            key = ''
            self.busy = False
            if self.is_mounted:
                for control in self.query(Select):
                    control.disabled = False
                self.query_one('#connection-submit',Button).disabled = False


class ConnectionApp(App):
    def __init__(self, home, language='en'):
        super().__init__()
        self.home,self.language=Path(home),language

    def on_mount(self):
        self.push_screen(ConnectionScreen(self.home,language=self.language),self.exit)


def main():
    import os, sys
    if not has_interactive_input():
        print('ONBOARDING_REQUIRED: Open interactive hashi onboard.', file=sys.stderr)
        return 78
    home=Path(os.environ.get('BRIDGE_HOME') or Path.cwd())
    result=ConnectionApp(home,os.environ.get('HASHI_UI_LANGUAGE','en')).run()
    return 0 if result else 130


if __name__=='__main__':
    raise SystemExit(main())
