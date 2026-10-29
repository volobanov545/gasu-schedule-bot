from __future__ import annotations

import ast
import copy
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

_SOURCE = Path(__file__).parents[1] / "tools/ayugram/gasu_ayu_probe.py"


class JavaList(list[Any]):
    def add(self, value: Any) -> None:
        self.append(value)


def tl_class(name: str, **fields: Any) -> type:
    def initialize(self: Any) -> None:
        for field, value in fields.items():
            setattr(self, field, copy.deepcopy(value))

    return type(name, (), {**fields, "__init__": initialize})


def iv_types() -> SimpleNamespace:
    return SimpleNamespace(
        TL_inputRichMessage=tl_class(
            "TL_inputRichMessage", flags=0, noautolink=False, rtl=False, blocks=JavaList(),
        ),
        textPlain=tl_class("textPlain", text=""),
        pageBlockParagraph=tl_class("pageBlockParagraph", text=None),
        pageBlockDetails=tl_class(
            "pageBlockDetails", flags=0, open=False, title=None, blocks=JavaList(),
        ),
        pageBlockTable=tl_class(
            "pageBlockTable", flags=0, title=None, rows=JavaList(),
            bordered=False, striped=False, compact=False,
        ),
        pageTableRow=tl_class("pageTableRow", cells=JavaList()),
        pageTableCell=tl_class(
            "pageTableCell", flags=0, text=None, header=False, align_center=False,
            valign_middle=False, colspan=0, rowspan=0,
        ),
    )


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    env = SimpleNamespace(
        requests=[], settings={}, notifications=[], processed=[], clients_called=[],
        selected=0, user_ids={0: 123, 1: 456}, iv=iv_types(),
    )

    class BasePlugin:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.logger = SimpleNamespace(
                info=lambda *a: None, warning=lambda *a: None, error=lambda *a: None,
            )

        def client(self, account: int) -> Any:
            env.clients_called.append(account)
            return SimpleNamespace(
                get_user_config=lambda: SimpleNamespace(
                    getCurrentUser=lambda: SimpleNamespace(id=env.user_ids[account]),
                ),
                send_request=lambda request, callback: env.requests.append(
                    (account, request, callback),
                ),
                get_messages_controller=lambda: SimpleNamespace(
                    processUpdates=lambda response, flag: env.processed.append(
                        (account, response, flag),
                    ),
                ),
            )

        def get_setting(self, key: str, default: Any = None) -> Any:
            return env.settings.get(key, default)

        def set_setting(self, key: str, value: Any, **kwargs: Any) -> None:
            env.settings[key] = value

    class Setting:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    class Bulletin:
        @staticmethod
        def show_info(text: str, *args: Any) -> None:
            env.notifications.append(text)

        show_success = show_info
        show_error = show_info

    user_config = SimpleNamespace(selectedAccount=0)
    tlrpc = SimpleNamespace(
        TL_inputPeerSelf=tl_class("TL_inputPeerSelf"),
        TL_messages_sendMessage=tl_class(
            "TL_messages_sendMessage", flags=0, peer=None, message="", random_id=0,
            rich_message=None, silent=False, no_webpage=False,
        ),
        TL_messages_editMessage=tl_class(
            "TL_messages_editMessage", flags=0, peer=None, id=0,
            message="", rich_message=None, no_webpage=False,
        ),
    )
    modules = {
        "base_plugin": {"BasePlugin": BasePlugin},
        "ui": {},
        "ui.settings": {"Header": Setting, "Text": Setting, "Divider": Setting},
        "ui.bulletin": {"BulletinHelper": Bulletin},
        "android_utils": {
            "run_on_ui_thread": lambda callback, *a, **kw: callback(),
        },
        "client_utils": {
            "get_selected_account": lambda: env.selected,
            "run_on_queue": lambda callback, *a, **kw: callback(),
        },
        "org": {},
        "org.telegram": {},
        "org.telegram.messenger": {
            "UserConfig": user_config,
            "BuildVars": SimpleNamespace(BUILD_VERSION_STRING="12.8.0"),
        },
        "org.telegram.tgnet": {"TLRPC": tlrpc},
        "org.telegram.tgnet.tl": {"TL_iv": env.iv},
    }
    for name, values in modules.items():
        module = ModuleType(name)
        module.__dict__.update(values)
        monkeypatch.setitem(sys.modules, name, module)

    spec = importlib.util.spec_from_file_location("gasu_ayu_probe_under_test", _SOURCE)
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    env.module = loaded
    env.plugin = loaded.GasuProbePlugin()
    env.plugin.on_plugin_load()
    env.user_config = user_config
    return env


def test_probe_metadata_is_literal_and_requires_account_safe_sdk() -> None:
    tree = ast.parse(_SOURCE.read_text(encoding="utf-8"))
    metadata = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.startswith("__"):
                    metadata[target.id] = ast.literal_eval(node.value)
    assert metadata["__id__"] == "gasu_ayu_probe"
    assert metadata["__sdk_version__"] == ">=1.4.5.0"
    assert metadata.get("__requirements__", []) == []


def test_distributable_is_the_exact_tested_source() -> None:
    artifact = _SOURCE.with_suffix(".plugin")
    assert artifact.read_bytes() == _SOURCE.read_bytes()


def test_installation_and_settings_are_passive(probe: SimpleNamespace) -> None:
    settings = probe.plugin.create_settings()
    probe.plugin.on_plugin_unload()
    assert settings
    assert probe.requests == []
    assert probe.processed == []
    assert probe.settings == {}


def test_compatibility_check_does_not_send(probe: SimpleNamespace) -> None:
    probe.plugin.check_compatibility()
    assert probe.requests == []


def test_native_rich_probe_contains_collapsible_centered_table(probe: SimpleNamespace) -> None:
    rich = probe.module.build_rich(probe.iv)
    assert rich.noautolink is True
    details = next(block for block in rich.blocks if type(block).__name__ == "pageBlockDetails")
    table = next(block for block in details.blocks if type(block).__name__ == "pageBlockTable")
    cells = [cell for row in table.rows for cell in row.cells]
    assert cells
    assert any(cell.header for cell in cells)
    assert all(cell.flags & (1 << 7) for cell in cells)
    assert all(cell.align_center and cell.valign_middle for cell in cells)
    assert any(cell.colspan > 1 or cell.rowspan > 1 for cell in cells)
    for cell in cells:
        if cell.colspan > 1:
            assert cell.flags & (1 << 1)
        if cell.rowspan > 1:
            assert cell.flags & (1 << 2)


@pytest.mark.parametrize("method", ["send_classic", "send_rich"])
def test_manual_send_uses_saved_messages_only(probe: SimpleNamespace, method: str) -> None:
    getattr(probe.plugin, method)()
    assert len(probe.requests) == 1
    account, request, _callback = probe.requests[0]
    assert account == 0
    assert type(request.peer).__name__ == "TL_inputPeerSelf"
    assert 0 < request.random_id < 2**63
    assert request.message == "" if method == "send_rich" else bool(request.message)
    assert request.rich_message is not None if method == "send_rich" else True


def test_missing_rich_types_do_not_fall_back_to_classic(probe: SimpleNamespace) -> None:
    delattr(probe.iv, "pageBlockTable")
    probe.plugin.send_rich()
    assert probe.requests == []
    assert probe.notifications


def test_no_overlapping_send_and_rpc_failure_does_not_retry(probe: SimpleNamespace) -> None:
    probe.plugin.send_rich()
    probe.plugin.send_rich()
    assert len(probe.requests) == 1
    _account, _request, callback = probe.requests[0]
    callback(None, SimpleNamespace(text="FLOOD_WAIT_60"))
    assert len(probe.requests) == 1
    assert probe.settings == {}


def test_rpc_error_never_displays_raw_secret_or_traceback(probe: SimpleNamespace) -> None:
    probe.plugin.send_rich()
    callback = probe.requests[0][2]
    callback(None, SimpleNamespace(text="secret-account-session-value"))
    assert "secret-account-session-value" not in str(probe.notifications)
    assert probe.settings == {}
    assert probe.processed == []


def test_edit_requires_probe_created_on_same_account(probe: SimpleNamespace) -> None:
    probe.settings["last_rich_probe"] = {"account": 1, "user_id": 456, "message_id": 777}
    probe.plugin.edit_last_rich()
    assert probe.requests == []
    probe.settings["last_rich_probe"] = {"account": 0, "user_id": 999, "message_id": 777}
    probe.plugin.edit_last_rich()
    assert probe.requests == []


def test_valid_edit_targets_only_own_saved_probe(probe: SimpleNamespace) -> None:
    probe.settings["last_rich_probe"] = {"account": 0, "user_id": 123, "message_id": 777}
    probe.plugin.edit_last_rich()
    assert len(probe.requests) == 1
    account, request, _ = probe.requests[0]
    assert account == 0
    assert type(request.peer).__name__ == "TL_inputPeerSelf"
    assert request.id == 777
    assert request.rich_message is not None


def test_send_response_id_must_correlate_to_probe(probe: SimpleNamespace) -> None:
    random_id = 223344
    other = tl_class("TL_updateMessageID", random_id=random_id + 1, id=888)()
    correlated = tl_class("TL_updateMessageID", random_id=random_id, id=777)()
    response = SimpleNamespace(updates=[other, correlated])
    assert probe.module.extract_message_id(response, random_id) == 777
    assert not probe.module.extract_message_id(SimpleNamespace(updates=[other]), random_id)
    # An incoming message ID is not proof of the result of our send.
    assert not probe.module.extract_message_id(
        SimpleNamespace(updates=[SimpleNamespace(message=SimpleNamespace(id=999))]), random_id,
    )
    assert not probe.module.extract_message_id(
        SimpleNamespace(updates=[SimpleNamespace(random_id=random_id, id=999)]), random_id,
    )


def test_short_sent_response_is_accepted_but_arbitrary_id_is_not(probe: SimpleNamespace) -> None:
    response = tl_class("TL_updateShortSentMessage", id=777)()
    assert probe.module.extract_message_id(response, 123) == 777
    assert not probe.module.extract_message_id(SimpleNamespace(id=999), 123)


def test_success_persists_only_probe_routing_metadata(probe: SimpleNamespace) -> None:
    probe.plugin.send_rich()
    account, request, callback = probe.requests[0]
    response = SimpleNamespace(updates=[
        tl_class("TL_updateMessageID", random_id=request.random_id, id=777)(),
    ])
    callback(response, None)
    assert probe.settings == {
        "last_rich_probe": {"account": account, "user_id": 123, "message_id": 777},
    }
    assert len(probe.requests) == 1


def test_callback_keeps_original_account_if_ui_switches(probe: SimpleNamespace) -> None:
    probe.plugin.send_rich()
    _account, request, callback = probe.requests[0]
    probe.user_config.selectedAccount = 1
    response = SimpleNamespace(updates=[
        tl_class("TL_updateMessageID", random_id=request.random_id, id=777)(),
    ])
    callback(response, None)
    assert probe.settings["last_rich_probe"]["account"] == 0
    assert probe.settings["last_rich_probe"]["user_id"] == 123
    assert probe.processed == [(0, response, False)]


def test_unconfirmed_send_id_never_enables_edit(probe: SimpleNamespace) -> None:
    probe.plugin.send_rich()
    callback = probe.requests[0][2]
    callback(SimpleNamespace(updates=[]), None)
    assert probe.settings == {}
    probe.plugin.edit_last_rich()
    assert len(probe.requests) == 1


def test_no_new_rpc_after_unload(probe: SimpleNamespace) -> None:
    probe.plugin.on_plugin_unload()
    probe.plugin.send_classic()
    probe.plugin.send_rich()
    probe.plugin.edit_last_rich()
    assert probe.requests == []


def test_late_callback_after_unload_does_not_update_settings_or_ui(
    probe: SimpleNamespace,
) -> None:
    probe.plugin.send_rich()
    callback = probe.requests[0][2]
    probe.plugin.on_plugin_unload()
    before = list(probe.notifications)
    callback(tl_class("TL_updateShortSentMessage", id=777)(), None)
    assert probe.settings == {}
    assert probe.processed == []
    assert probe.notifications == before
