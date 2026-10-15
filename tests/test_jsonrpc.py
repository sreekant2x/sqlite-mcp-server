import pytest

from sqlite_mcp import jsonrpc
from sqlite_mcp.jsonrpc import INVALID_REQUEST, PARSE_ERROR, JsonRpcError


def test_parses_request_notification_and_response():
    req = jsonrpc.parse('{"jsonrpc":"2.0","id":7,"method":"ping"}')
    assert req == jsonrpc.Request(id=7, method="ping", params={})

    note = jsonrpc.parse('{"jsonrpc":"2.0","method":"notifications/initialized"}')
    assert note == jsonrpc.Notification(method="notifications/initialized", params={})

    resp = jsonrpc.parse('{"jsonrpc":"2.0","id":"a","result":{}}')
    assert resp == jsonrpc.Response(id="a", result={})


@pytest.mark.parametrize(
    "raw, code",
    [
        ("not json", PARSE_ERROR),
        ("[]", INVALID_REQUEST),
        ('"a string"', INVALID_REQUEST),
        ('{"id":1,"method":"ping"}', INVALID_REQUEST),
        ('{"jsonrpc":"1.0","id":1,"method":"ping"}', INVALID_REQUEST),
        ('{"jsonrpc":"2.0","id":1,"method":5}', INVALID_REQUEST),
        ('{"jsonrpc":"2.0","id":null,"method":"ping"}', INVALID_REQUEST),
        ('{"jsonrpc":"2.0","id":true,"method":"ping"}', INVALID_REQUEST),
        ('{"jsonrpc":"2.0","id":1,"method":"ping","params":[1,2]}', INVALID_REQUEST),
        ('{"jsonrpc":"2.0","id":1}', INVALID_REQUEST),
    ],
)
def test_rejects_malformed_messages(raw, code):
    with pytest.raises(JsonRpcError) as info:
        jsonrpc.parse(raw)
    assert info.value.code == code


def test_invalid_request_keeps_id_so_it_can_be_answered():
    with pytest.raises(JsonRpcError) as info:
        jsonrpc.parse('{"jsonrpc":"2.0","id":9,"method":"ping","params":"x"}')
    assert info.value.request_id == 9


def test_encode_is_single_line():
    line = jsonrpc.encode(jsonrpc.success(1, {"text": "a\nb"}))
    assert "\n" not in line
