import ast
import re
from collections.abc import Callable, Iterator
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from eth_account.signers.local import LocalAccount

import hl_guard
from hl_guard.exchange import transport
from hl_guard.exchange.actions import FORBIDDEN_ACTION_TYPES
from hl_guard.exchange.halt import HaltFlag
from hl_guard.exchange.signer import Signer, SignerError
from hl_guard.exchange.transport import ExchangeTransport

PACKAGE = Path(hl_guard.__file__).resolve().parent
SIGNER = PurePosixPath("exchange/signer.py")
ACTIONS = PurePosixPath("exchange/actions.py")
APPROVE_PAGE = PurePosixPath("keys/approve_page.py")

SIGNER_ONLY_NAMES = {
    "sign_typed_data",
    "sign_message",
    "_sign_hash",
    "hash_eip712_message",
    "sign_user_signed_action",
    "encode_typed_data",
    "unsafe_sign_hash",
    "sign_msg_hash",
}
NAME_ARGUMENT = {
    "getattr": 1,
    "attrgetter": 0,
    "methodcaller": 0,
    "__import__": 0,
    "import_module": 0,
}
USER_SIGNED_CHAIN_ID = 0x66EEE
USER_SIGNED_TEXT = re.compile(
    r"hyperliquidsigntransaction|hyperliquidtransaction:\w*|0x66eee|signaturechainid"
    r"|sign_user_signed_action|user_signed_payload|usd_transfer|approve_agent|\bwithdraw\b"
    r"|sign_\w*(?:withdraw|transfer|send_asset|approve|agent|delegate|multi_sig)\w*"
    r"|hyperliquid\.exchange",
    re.IGNORECASE,
)
APPROVE_PAGE_ALLOWED_TEXT = {
    "hyperliquidsigntransaction",
    "hyperliquidtransaction:approveagent",
    "0x66eee",
    "signaturechainid",
    "approve_agent",
}
FORBIDDEN_NAMES = {name.lower() for name in FORBIDDEN_ACTION_TYPES}
SCOPE = (
    "the scan catches accidental or copy-pasted user-signed code in .py files; "
    "deliberate obfuscation is out of its scope and is left to the runtime refusal "
    "in Signer.sign_action"
)


def _folded(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _folded(node.left), _folded(node.right)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.FormattedValue) and node.format_spec is None:
        return _folded(node.value)
    if isinstance(node, ast.JoinedStr):
        parts = [_folded(v) for v in node.values]
        return None if None in parts else "".join(p for p in parts if p is not None)
    return None


def _names(node: ast.AST) -> Iterator[str]:
    if isinstance(node, ast.Name):
        yield node.id
    elif isinstance(node, ast.Attribute):
        yield node.attr
    elif isinstance(node, ast.alias):
        yield from node.name.split(".")
    elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        yield node.name


def _modules(node: ast.AST) -> Iterator[str]:
    if isinstance(node, ast.Import):
        yield from (alias.name for alias in node.names)
    elif isinstance(node, ast.ImportFrom) and node.module:
        yield node.module


def _type_name_allowed(rel: PurePosixPath, lowered: str) -> bool:
    return rel == ACTIONS or (rel == APPROVE_PAGE and lowered == "approveagent")


def _ast_hits(rel: PurePosixPath, tree: ast.AST) -> Iterator[str]:
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        for module in _modules(node):
            if module == "hyperliquid" or module.startswith("hyperliquid."):
                yield f"{line}: imports the Hyperliquid SDK ({module})"
            if rel == APPROVE_PAGE and module.split(".")[0] in {"eth_account", "eth_keys"}:
                yield f"{line}: the approve page imports a signing library ({module})"
        for name in _names(node):
            if name.lower() in SIGNER_ONLY_NAMES and rel != SIGNER:
                yield f"{line}: signing API {name} outside {SIGNER}"
            if rel == APPROVE_PAGE and name.lower().startswith("sign"):
                yield f"{line}: the approve page names a signing call ({name})"
        if isinstance(node, ast.Call):
            callee = next(_names(node.func), "")
            index = NAME_ARGUMENT.get(callee)
            if index is not None and len(node.args) > index and _folded(node.args[index]) is None:
                yield f"{line}: {callee} with a computed name"
        if (
            isinstance(node, ast.Constant)
            and type(node.value) is int
            and node.value == USER_SIGNED_CHAIN_ID
            and rel != APPROVE_PAGE
        ):
            yield f"{line}: the user-signed chain id {USER_SIGNED_CHAIN_ID:#x}"
        text = _folded(node)
        if text is None:
            continue
        built = not isinstance(node, ast.Constant)
        lowered = text.lower()
        if lowered in SIGNER_ONLY_NAMES and rel != SIGNER:
            yield f"{line}: signing API name {text!r} as a string"
        if re.match(r"hyperliquid(\.|$)", text):
            yield f"{line}: names the Hyperliquid SDK module {text!r}"
        if lowered in FORBIDDEN_NAMES and (built or not _type_name_allowed(rel, lowered)):
            yield f"{line}: forbidden action type {text!r}"
        for token in USER_SIGNED_TEXT.findall(text):
            if built or not (rel == APPROVE_PAGE and token.lower() in APPROVE_PAGE_ALLOWED_TEXT):
                yield f"{line}: user-signed token {token!r} in a string"


def _text_hits(rel: PurePosixPath, source: str) -> Iterator[str]:
    for lineno, line in enumerate(source.splitlines(), 1):
        for token in USER_SIGNED_TEXT.findall(line):
            if not (rel == APPROVE_PAGE and token.lower() in APPROVE_PAGE_ALLOWED_TEXT):
                yield f"{lineno}: user-signed token {token!r}"


def scan(root: Path) -> list[str]:
    hits: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = PurePosixPath(path.relative_to(root).as_posix())
        source = path.read_text(encoding="utf-8")
        found = [*_text_hits(rel, source), *_ast_hits(rel, ast.parse(source))]
        hits.extend(f"{rel}:{hit}" for hit in dict.fromkeys(found))
    return hits


def test_the_package_has_no_user_signed_path() -> None:
    assert scan(PACKAGE) == [], SCOPE


def _plant(root: Path, rel: str, source: str) -> list[str]:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    return scan(root)


BYPASSES = {
    "computed getattr": (
        "exchange/planted.py",
        "import eth_account\nf = getattr(eth_account, 'sign_user_' + 'signed_action')\n",
    ),
    "getattr of a variable": (
        "exchange/planted.py",
        "import eth_account\nname = load()\nf = getattr(eth_account, name)\n",
    ),
    "built usdSend": ("exchange/planted.py", 'action = {"type": "usd" + "Send"}\n'),
    "built usdSend in actions": ("exchange/actions.py", 'action = {"type": "usd" + "Send"}\n'),
    "f-string withdraw3": ("exchange/planted.py", "kind = f\"with{'draw'}3\"\n"),
    "plain forbidden type": ("exchange/transport.py", 'action = {"type": "withdraw3"}\n'),
    "sdk-only type": ("cli/planted.py", 'action = {"type": "subAccountTransfer"}\n'),
    "sign_typed_data withdraw": (
        "exchange/planted.py",
        "from eth_account import Account\n"
        "Account.sign_typed_data(key, full_message={'domain': {'name': 'Hyperliquid' + "
        "'SignTransaction', 'chainId': 0x66eee}, 'primaryType': 'Hyperliquid' + "
        "'Transaction:Withdraw'})\n",
    ),
    "typed data in signer": (
        "exchange/signer.py",
        "D = {'primaryType': 'HyperliquidTransaction:UsdSend'}\n",
    ),
    "chain id as an int": ("exchange/signer.py", "CHAIN = 421614\n"),
    "lower-case domain": ("exchange/planted.py", "NAME = 'hyperliquidsigntransaction'\n"),
    "upper-case chain id": ("exchange/planted.py", "CHAIN = '0X66EEE'\n"),
    "aliased encode_typed_data": (
        "exchange/probe.py",
        "from eth_account.messages import encode_typed_data as e\n",
    ),
    "typed-data name as a string": (
        "exchange/planted.py",
        "import operator\nf = operator.attrgetter('sign_typed_data')\n",
    ),
    "unsafe_sign_hash outside the signer": (
        "exchange/planted.py",
        "from eth_account import Account\nsig = Account.unsafe_sign_hash(digest, key)\n",
    ),
    "sign_msg_hash outside the signer": ("cli/planted.py", "sig = key.sign_msg_hash(digest)\n"),
    "sign_message outside the signer": (
        "server/planted.py",
        "sig = agent.sign_message(signable)\n",
    ),
    "_sign_hash outside the signer": (
        "keys/planted.py",
        "from eth_account import Account\nsig = Account._sign_hash(digest, key)\n",
    ),
    "hash_eip712_message outside the signer": (
        "cli/planted.py",
        "from eth_account._utils.structured_data.hashing import hash_eip712_message\n",
    ),
    "sdk signing import": ("exchange/planted.py", "from hyperliquid.utils import signing\n"),
    "sdk import by name": (
        "exchange/planted.py",
        "import importlib\nm = importlib.import_module('hyperliquid.utils.signing')\n",
    ),
    "computed import": (
        "exchange/planted.py",
        "import importlib\nm = importlib.import_module('hyper' + name)\n",
    ),
    "withdraw helper under keys": (
        "keys/bridge.py",
        "def sign_withdraw_from_bridge_action():\n    ...\n",
    ),
    "withdraw helper in the approve page": (
        "keys/approve_page.py",
        "def sign_withdraw_from_bridge_action():\n    ...\n",
    ),
    "approve page signs": (
        "keys/approve_page.py",
        "from eth_account import Account\nsig = Account.sign_message(m, key)\n",
    ),
    "approve page builds a withdrawal": (
        "keys/approve_page.py",
        "PRIMARY = 'HyperliquidTransaction:Withdraw'\n",
    ),
    "approve page builds a forbidden type": (
        "keys/approve_page.py",
        'ACTION = {"type": "usdSend"}\n',
    ),
}


@pytest.mark.parametrize(("rel", "source"), BYPASSES.values(), ids=BYPASSES.keys())
def test_the_scan_catches_each_bypass(tmp_path: Path, rel: str, source: str) -> None:
    assert _plant(tmp_path, rel, source), SCOPE


def test_the_signer_may_sign_a_hash(tmp_path: Path) -> None:
    source = (
        "sig = Account.unsafe_sign_hash(digest, key)\nsig = key.sign_msg_hash(digest)\n"
        "sig = agent.sign_message(signable)\n"
    )
    assert _plant(tmp_path, "exchange/signer.py", source) == []


def test_the_scan_catches_the_round_one_bypass_file(tmp_path: Path) -> None:
    source = (
        "from hyperliquid.utils import signing\n"
        "from eth_account import Account\n"
        'f = getattr(signing, "sign_user_" + "signed_action")\n'
        "Account.sign_typed_data(key, full_message={'domain': {'name': "
        "'HyperliquidSignTransaction', 'chainId': 0x66eee}, "
        "'primaryType': 'HyperliquidTransaction:Withdraw'})\n"
        'action = {"type": "usd" + "Send"}\n'
    )
    hits = _plant(tmp_path, "exchange/planted.py", source)
    assert {int(hit.split(":")[1]) for hit in hits} == {1, 3, 4, 5}


@pytest.mark.parametrize(
    "name",
    [
        "sign_user_signed_action",
        "withdraw",
        "usd_transfer",
        "approve_agent",
        "sign_withdraw_from_bridge_action",
        "sign_usd_class_transfer_action",
        "sign_send_asset_action",
        "sign_approve_builder_fee",
        "sign_agent",
        "sign_token_delegate_action",
        "sign_convert_to_multi_sig_user_action",
        "Withdraw",
        "SIGN_USER_SIGNED_ACTION",
    ],
)
def test_the_scan_catches_each_helper_name(tmp_path: Path, name: str) -> None:
    assert _plant(tmp_path, "exchange/planted.py", f"{name} = None\n")


def test_the_approve_page_may_describe_the_approve_agent_typed_data(tmp_path: Path) -> None:
    source = (
        "def approve_agent_typed_data(agent, nonce):\n"
        "    return {\n"
        "        'domain': {'name': 'HyperliquidSignTransaction', 'chainId': 0x66eee},\n"
        "        'primaryType': 'HyperliquidTransaction:ApproveAgent',\n"
        "        'message': {'type': 'approveAgent', 'signatureChainId': '0x66eee'},\n"
        "    }\n"
    )
    assert _plant(tmp_path, "keys/approve_page.py", source) == []


def test_the_exemption_covers_only_the_approve_page(tmp_path: Path) -> None:
    source = "PRIMARY = 'HyperliquidTransaction:ApproveAgent'\n"
    assert _plant(tmp_path, "keys/approve.py", source)


def test_the_forbidden_list_itself_is_allowed_in_actions(tmp_path: Path) -> None:
    source = 'FORBIDDEN = {"withdraw3", "cWithdraw", "approveAgent", "usdSend"}\n'
    assert _plant(tmp_path, "exchange/actions.py", source) == []


REFUSED_TYPES = sorted(FORBIDDEN_ACTION_TYPES | {"Withdraw3", "usdsend", "sendMultiSig"})
USER_SIGNED_FIELDS = {
    "hyperliquidChain": "Testnet",
    "signatureChainId": "0x66eee",
    "destination": "0x" + "00" * 20,
    "amount": "1",
    "time": 1,
}


@pytest.mark.parametrize("action_type", REFUSED_TYPES)
def test_every_refused_type_is_stopped_before_the_key(
    key_watch: Callable[[LocalAccount], Any],
    agent: LocalAccount,
    master: str,
    durable_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    action_type: str,
) -> None:
    watched = key_watch(agent)
    signer = Signer(watched, master, network="testnet")
    tx = ExchangeTransport(signer, "testnet", HaltFlag(durable_dir))
    posts: list[Any] = []
    monkeypatch.setattr(transport._OPENER, "open", lambda *a, **k: posts.append(a))
    for action in ({"type": action_type}, {"type": action_type, **USER_SIGNED_FIELDS}):
        with pytest.raises(SignerError):
            signer.sign_action(action, nonce=1)
        with pytest.raises(SignerError):
            tx.send(action, nonce=1)
    assert watched.touches == 0
    assert posts == []
    assert tx.last_request is None


def test_user_signed_fields_on_an_order_are_refused_before_the_key(
    key_watch: Callable[[LocalAccount], Any],
    agent: LocalAccount,
    master: str,
    order_action: Callable[[], Any],
) -> None:
    watched = key_watch(agent)
    signer = Signer(watched, master, network="testnet")
    for field, value in USER_SIGNED_FIELDS.items():
        with pytest.raises(SignerError):
            signer.sign_action({**order_action(), field: value}, nonce=1)
    assert watched.touches == 0
