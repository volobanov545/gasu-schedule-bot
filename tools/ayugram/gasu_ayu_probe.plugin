"""Manual Saved Messages probe; no hooks, timers, account secrets or group writes."""

import re
import secrets

from base_plugin import BasePlugin
from org.telegram.messenger import UserConfig
from org.telegram.tgnet import TLRPC
from ui.bulletin import BulletinHelper
from ui.settings import Divider, Header, Text

try:
    from org.telegram.tgnet.tl import TL_iv
except ImportError:
    TL_iv = None

__id__ = "gasu_ayu_probe"
__name__ = "GASU — проверка отправки"
__description__ = "Ручная проверка только в Избранном. Группы не затрагиваются."
__author__ = "GASU Schedule"
__version__ = "1.0.0"
__icon__ = "exteraPlugins/1"
__app_version__ = ">=12.5.1"
__sdk_version__ = ">=1.4.5.0"

STATE_KEY = "last_rich_probe"


def _plain(iv, value):
    text = iv.textPlain()
    text.text = value
    return text


def build_rich(iv, updated=False):
    """Construct Android native rich blocks, not unsupported HTML strings."""

    rich = iv.TL_inputRichMessage()
    rich.noautolink = True
    paragraph = iv.pageBlockParagraph()
    paragraph.text = _plain(
        iv,
        "Обновление этой карточки работает." if updated
        else "Проверка AyuGram. Это демонстрация, не расписание группы.",
    )
    rich.blocks.add(paragraph)
    table = iv.pageBlockTable()
    table.title = _plain(iv, "Пример оформления")
    table.bordered = True
    table.striped = False
    table.compact = True

    def cell(value, *, header=False, colspan=1, rowspan=1):
        item = iv.pageTableCell()
        item.flags = 1 << 7  # Rich text is optional in the TL cell schema.
        item.text = _plain(iv, value)
        item.header = header
        item.align_center = True
        item.valign_middle = True
        if colspan > 1:
            item.colspan = colspan
            item.flags |= 1 << 1
        if rowspan > 1:
            item.rowspan = rowspan
            item.flags |= 1 << 2
        return item

    def row(*items):
        item = iv.pageTableRow()
        for value in items:
            item.cells.add(value)
        table.rows.add(item)

    for time, subject, kind, room, building, teacher in (
        ("09:00\n10:30", "Строймех", "Практика", "105", "К", "Иванов Иван Иванович"),
        ("10:45\n12:15", "СМС", "Лекция", "205", "С", "Петрова Анна Сергеевна"),
    ):
        row(cell(time, rowspan=4), cell(subject, colspan=3))
        row(cell("Тип", header=True), cell("Ауд.", header=True), cell("Корп.", header=True))
        row(cell(kind), cell(room), cell(building))
        row(cell(teacher, colspan=3))
    details = iv.pageBlockDetails()
    details.title = _plain(iv, "Раскрыть пробную таблицу")
    details.open = False
    details.blocks.add(table)
    rich.blocks.add(details)
    return rich


def _class_name(value):
    try:
        return str(value.getClass().getSimpleName())
    except (AttributeError, TypeError):
        return type(value).__name__.rsplit("$", 1)[-1]


def extract_message_id(response, random_id):
    """Accept only this send's short response or correlated random-id mapping."""

    if _class_name(response) == "TL_updateShortSentMessage":
        message_id = int(response.id)
        return message_id if message_id > 0 else None
    for update in getattr(response, "updates", ()):
        if (
            _class_name(update) == "TL_updateMessageID"
            and int(update.random_id) == random_id
        ):
            message_id = int(update.id)
            return message_id if message_id > 0 else None
    return None


def _compatibility_missing():
    required = {
        "TL_inputRichMessage": ("noautolink", "blocks"),
        "textPlain": ("text",),
        "pageBlockParagraph": ("text",),
        "pageBlockDetails": ("open", "title", "blocks"),
        "pageBlockTable": ("bordered", "striped", "compact", "title", "rows"),
        "pageTableRow": ("cells",),
        "pageTableCell": (
            "flags", "text", "header", "align_center", "valign_middle", "colspan", "rowspan",
        ),
    }
    missing = []
    for name, fields in required.items():
        try:
            instance = getattr(TL_iv, name)()
            missing.extend(f"TL_iv.{name}.{field}" for field in fields
                           if not hasattr(instance, field))
        except (AttributeError, TypeError):
            missing.append(f"TL_iv.{name}")
    for name in ("TL_messages_sendMessage", "TL_messages_editMessage"):
        try:
            if not hasattr(getattr(TLRPC, name)(), "rich_message"):
                missing.append(f"TLRPC.{name}.rich_message")
        except (AttributeError, TypeError):
            missing.append(f"TLRPC.{name}")
    return missing


class GasuProbePlugin(BasePlugin):
    def on_plugin_load(self):
        self._active = True
        self._pending = False
        self._status = "Ничего не отправлялось. Выберите проверку ниже."

    def on_plugin_unload(self):
        self._active = False

    def create_settings(self):
        return [
            Header(text="Проверка только в Избранном"),
            Text(text="Совместимость карточки", on_click=self.check_compatibility),
            Text(text="1. Отправить простой тест", on_click=self.send_classic),
            Text(text="2. Отправить таблицу и раскрытие", on_click=self.send_rich),
            Text(text="3. Обновить последнюю пробную карточку", on_click=self.edit_last_rich),
            Divider(text="При установке и включении плагин ничего не отправляет."),
            Text(text="Результат", subtext=getattr(self, "_status", "Проверка не запускалась.")),
        ]

    def _report(self, text, error=False):
        self._status = text
        try:
            if error:
                BulletinHelper.show_error(text)
            else:
                BulletinHelper.show_info(text)
        except Exception:
            return

    def check_compatibility(self, _view=None):
        missing = _compatibility_missing()
        if missing:
            self._report("Нет поддержки rich-карточки: " + ", ".join(missing), True)
        else:
            self._report("Нужные классы есть. Отправка ещё не проверена.")

    def send_classic(self, _view=None):
        self._send(False)

    def send_rich(self, _view=None):
        self._send(True)

    def _context(self):
        account = int(UserConfig.selectedAccount)
        client = self.client(account)
        me = client.get_user_config().getCurrentUser()
        user_id = int(me.id) if me is not None else 0
        if user_id <= 0:
            raise ValueError("NO_ACCOUNT")
        return account, user_id, client

    def _begin(self, needs_rich):
        if not getattr(self, "_active", False):
            return None
        if self._pending:
            self._report("Предыдущая проверка ещё выполняется.")
            return None
        if needs_rich and _compatibility_missing():
            self.check_compatibility()
            return None
        try:
            context = self._context()
        except Exception:
            self._report("Не удалось выбрать вошедший аккаунт.", True)
            return None
        self._pending = True
        return context

    def _send(self, rich):
        context = self._begin(rich)
        if context is None:
            return
        account, user_id, client = context
        try:
            request = TLRPC.TL_messages_sendMessage()
            request.peer = TLRPC.TL_inputPeerSelf()
            request.random_id = secrets.randbits(63) or 1
            request.silent = True
            request.no_webpage = True
            request.message = "" if rich else "Проверка GASU: отправка от аккаунта работает."
            if rich:
                request.rich_message = build_rich(TL_iv)
            random_id = int(request.random_id)

            def complete(response, error):
                self._complete(
                    account, user_id, client, response, error,
                    random_id=random_id, remember=rich,
                )

            client.send_request(request, complete)
        except Exception:
            self._pending = False
            self._report("Тест не подтверждён. Повторной отправки не будет.", True)

    def edit_last_rich(self, _view=None):
        context = self._begin(True)
        if context is None:
            return
        account, user_id, client = context
        state = self.get_setting(STATE_KEY, {})
        if (
            not isinstance(state, dict) or state.get("account") != account
            or state.get("user_id") != user_id
            or not isinstance(state.get("message_id"), int)
            or isinstance(state["message_id"], bool) or state["message_id"] <= 0
        ):
            self._pending = False
            self._report("Сначала отправьте rich-тест с этого же аккаунта.", True)
            return
        try:
            request = TLRPC.TL_messages_editMessage()
            request.peer = TLRPC.TL_inputPeerSelf()
            request.id = state["message_id"]
            request.rich_message = build_rich(TL_iv, updated=True)

            def complete(response, error):
                self._complete(account, user_id, client, response, error, editing=True)

            client.send_request(request, complete)
        except Exception:
            self._pending = False
            self._report("Обновление не подтверждено. Повтора не будет.", True)

    def _complete(
        self, account, user_id, client, response, error,
        random_id=None, remember=False, editing=False,
    ):
        self._pending = False
        if not getattr(self, "_active", False):
            return
        if error is not None:
            code = str(getattr(error, "text", "RPC_ERROR"))
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code):
                code = "RPC_ERROR"
            self._report("Telegram вернул " + code + ". Автоповтора нет.", True)
            return
        if response is None:
            self._report("Нет подтверждения Telegram. Автоповтора нет.", True)
            return
        message_id = extract_message_id(response, random_id) if remember else None
        if message_id is not None:
            self.set_setting(STATE_KEY, {
                "account": account, "user_id": user_id, "message_id": message_id,
            })
        try:
            client.get_messages_controller().processUpdates(response, False)
        except Exception:
            self._report("Telegram принял запрос. Откройте Избранное для проверки.")
            return
        if editing:
            self._report("Telegram подтвердил обновление. Проверьте ту же карточку в Избранном.")
        elif remember and message_id is None:
            self._report("Запрос принят, но ID не подтверждён: редактирование не включено.")
        else:
            self._report("Telegram принял тест. Проверьте сообщение в Избранном.")
