"""Клиент модели (повторы, лимиты, «Стоп», max_tokens, список моделей) и Markdown-отчёт."""

import json
import re
import time

import pytest
import requests

from anydesk_helper.config import Settings
from anydesk_helper.llm import LLM, LLMError
from anydesk_helper.report import ticket_report

from .fakes import Clock


class Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self.body = body
        self.text = json.dumps(body) if body is not None else ""
        self.headers = requests.structures.CaseInsensitiveDict(headers or {})

    def json(self):
        if self.body is None:
            raise ValueError("no json")
        return self.body


def ok(content="{}", finish="stop"):
    return Resp(200, {"choices": [{"message": {"content": content}, "finish_reason": finish}]})


class Http:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.posts = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append(dict(json))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def get(self, url, headers=None, timeout=None):
        return self.responses.pop(0)


def make(*responses, **settings):
    s = Settings(api_key="k")
    for key, value in settings.items():
        setattr(s, key, value)
    clock = Clock()
    llm = LLM(s, session=Http(*responses), sleep=clock.sleep)
    return llm, llm.http, clock


RATE = Resp(429, {"error": {"message": "Rate limit exceeded: free-models-per-min."}})


def daily_limit():
    reset = int((time.time() + 5 * 3600) * 1000)
    return Resp(429, {"error": {"code": 429, "message": "Rate limit exceeded: free-models-per-day. Add 10 credits",
                                "metadata": {"headers": {"X-RateLimit-Limit": "50", "X-RateLimit-Remaining": "0",
                                                         "X-RateLimit-Reset": str(reset)}}}})


def test_retry_without_sleep_after_last_attempt():
    llm, http, clock = make(RATE, RATE, RATE, RATE)
    with pytest.raises(LLMError, match="HTTP 429"):
        llm.chat([])
    assert len(http.posts) == 4 and clock.t == 5 + 10 + 15


def test_retry_then_success():
    llm, http, clock = make(RATE, Resp(503, {"error": {"message": "busy"}}), ok("готово"))
    assert llm.chat([]) == "готово" and clock.t == 15


def test_daily_limit_fails_fast_with_reset_time():
    llm, http, clock = make(daily_limit())
    with pytest.raises(LLMError) as e:
        llm.chat([])
    assert len(http.posts) == 1 and clock.t == 0
    assert "дневной лимит" in str(e.value) and re.search(r"сброс около \d\d:\d\d", str(e.value))


def test_daily_limit_switches_to_fallback_model():
    llm, http, clock = make(daily_limit(), ok("ответ"), fallback_model="qwen/qwen-plus")
    notes = []
    llm.notify = notes.append
    assert llm.chat([]) == "ответ" and clock.t == 0
    assert [p["model"] for p in http.posts] == [Settings().model, "qwen/qwen-plus"]
    assert notes and "qwen/qwen-plus" in notes[0]


def test_retry_after_header_is_used_and_long_wait_fails_fast():
    llm, http, clock = make(Resp(429, {"error": {"message": "slow down"}}, {"Retry-After": "2"}), ok())
    llm.chat([])
    assert clock.t == 2
    llm, http, clock = make(Resp(429, {"error": {"message": "slow down"}}, {"Retry-After": "3600"}))
    with pytest.raises(LLMError, match="60 мин"):
        llm.chat([])
    assert len(http.posts) == 1 and clock.t == 0


def test_auth_and_bad_url_are_not_retried():
    llm, http, clock = make(Resp(401, {"error": {"message": "No auth"}}))
    with pytest.raises(LLMError, match="API-ключ"):
        llm.chat([])
    assert len(http.posts) == 1 and clock.t == 0
    llm, http, clock = make(requests.exceptions.MissingSchema("No scheme supplied"))
    with pytest.raises(LLMError, match="https://"):
        llm.chat([])
    assert len(http.posts) == 1 and clock.t == 0


def test_connection_error_is_retried():
    llm, http, clock = make(requests.exceptions.ConnectionError("down"), ok("да"))
    assert llm.chat([]) == "да" and clock.t == 5


def test_json_mode_fallback():
    llm, http, _ = make(Resp(400, {"error": {"message": "response_format is not supported"}}), ok())
    llm.chat([])
    assert "response_format" in http.posts[0] and "response_format" not in http.posts[1]


def test_stop_interrupts_backoff():
    llm, http, clock = make(RATE, RATE, RATE, RATE)
    llm.should_stop = lambda: clock.t >= 1
    with pytest.raises(LLMError, match="Остановлено техником"):
        llm.chat([])
    assert len(http.posts) == 1 and clock.t < 2


def test_stop_before_request():
    llm, http, _ = make(ok())
    llm.should_stop = lambda: True
    with pytest.raises(LLMError, match="Остановлено"):
        llm.chat([])
    assert http.posts == []


def test_empty_reply_cut_by_max_tokens_raises_limit():
    empty = Resp(200, {"choices": [{"message": {"content": None, "reasoning": "думаю…"}, "finish_reason": "length"}]})
    llm, http, clock = make(empty, ok("ответ"), max_tokens=1200)
    assert llm.chat([]) == "ответ" and clock.t == 0
    assert [p["max_tokens"] for p in http.posts] == [1200, 4096] and llm.max_tokens == 4096
    llm, http, _ = make(empty, max_tokens=8192)
    with pytest.raises(LLMError, match="max_tokens=8192"):
        llm.chat([])
    assert len(http.posts) == 1


def test_truncated_reply_is_returned_with_finish_reason():
    """Обрезанный ответ агент не выполняет (finish_reason) и переспрашивает — уже с бо́льшим лимитом."""
    llm, http, _ = make(ok('{"action": "ru', finish="length"), ok('{"action": "done"}'), max_tokens=1200)
    notes = []
    llm.notify = notes.append
    assert llm.chat([]) == '{"action": "ru' and llm.finish_reason == "length"
    assert llm.max_tokens == 4096 and "4096" in notes[0]
    assert llm.chat([]) == '{"action": "done"}' and llm.finish_reason == "stop"
    assert [p["max_tokens"] for p in http.posts] == [1200, 4096]
    llm, _, _ = make(ok('{"a": 1', finish="length"), max_tokens=8192)  # уже на пределе — лимит не растёт
    assert llm.chat([]) == '{"a": 1' and llm.max_tokens == 8192


def test_model_check_rejects_reply_cut_by_max_tokens():
    from anydesk_helper.checks import check_model
    s = Settings(api_key="k")
    s.vision = False
    llm, _, _ = make(ok('{"status": "ok"}', finish="length"))
    good, report = check_model(llm, s)
    assert not good and "max_tokens" in report[-1]
    llm, _, _ = make(ok('{"status": "ok"}'))
    assert check_model(llm, s)[0]


def test_list_models_free_flag_is_boolean():
    data = {"data": [{"id": "qwen-max"}, {"id": "qwen-vl-max"},
                     {"id": "qwen/qwen3-8b:free"},
                     {"id": "qwen/qwen-turbo", "pricing": {"prompt": "0.000", "completion": 0}},
                     {"id": "qwen/qwen-plus", "pricing": {"prompt": "0.0000004", "completion": "0.0000012"}}]}

    class Get(Resp):
        def raise_for_status(self):
            pass

    llm, _, _ = make(Get(200, data))
    free = {m["id"]: m["free"] for m in llm.list_models()}
    assert free == {"qwen-max": False, "qwen-vl-max": False, "qwen/qwen3-8b:free": True,
                    "qwen/qwen-turbo": True, "qwen/qwen-plus": False}


# ------------------------------------------------------------ отчёт


def outside_fences(md: str) -> list[str]:
    """Строки отчёта вне кодблоков; заодно проверяем, что каждый блок закрыт."""
    out, fence = [], None
    for line in md.splitlines():
        s = line.strip()
        if fence:
            if s == fence:
                fence = None
        elif m := re.match(r"(`{3,})", s):
            fence = m.group(1)
        else:
            out.append(line)
    assert fence is None, "кодблок не закрыт"
    return out


def test_report_fences_multiline_commands_and_output():
    t = {"id": 7, "title": "Принтер", "status": "solved", "anydesk_id": "1", "created_at": "c", "updated_at": "u",
         "description": "", "plan": [], "summary": ""}
    command = "# Остановить спулер\nStop-Service Spooler -Force `\n  -ErrorAction SilentlyContinue\nWrite-Host \"a`tb\"\n- x"
    events = [
        {"ts": "2026-01-01T10:00:00", "kind": "command", "text": "",
         "data": {"command": command, "status": "ok", "exit_code": 0, "output": "x\n```\n# y"}},
        {"ts": "2026-01-01T10:00:01", "kind": "ask", "text": "Ошибка модели\n# не заголовок", "data": {}},
    ]
    md = ticket_report(t, events)
    rest = outside_fences(md)
    assert [line for line in rest if line.startswith("#")] == ["# Тикет #7: Принтер", "## Ход решения"]
    assert not any(line.startswith("- x") for line in rest)
    assert "  ````" in md  # ограда длиннее ``` из вывода
    assert "Stop-Service Spooler -Force `" in md and "Команда → ok, код 0" in md
