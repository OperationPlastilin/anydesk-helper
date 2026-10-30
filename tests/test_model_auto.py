"""Автовыбор модели, замена пропавшей модели, мгновенный «Стоп» и скриншот при молчащей консоли."""

import threading
import time

import pytest

from anydesk_helper.agent import YES, StopSession
from anydesk_helper.config import Settings
from anydesk_helper.llm import LLM, LLMError

from .fakes import Clock
from .test_agent import make_agent
from .test_audit_d import Http, Resp, ok


def models(*items):
    """Ответ /models в формате OpenRouter: (id, vision, free)."""
    data = [{"id": mid, "architecture": {"input_modalities": ["text", "image"] if vision else ["text"]},
             "pricing": {"prompt": "0" if free else "0.000001", "completion": "0" if free else "0.000002"}}
            for mid, vision, free in items]
    r = Resp(200, {"data": data})
    r.raise_for_status = lambda: None
    return r


CATALOG = models(("meta/llama-vision:free", True, True), ("qwen/qwen-text:free", False, True),
                 ("qwen/qwen3-vl-30b:free", True, True), ("qwen/qwen2.5-vl-72b-instruct", True, False))

GONE = Resp(404, {"error": {"message": "This model is unavailable for free. The paid version is available "
                                       "now - use this slug instead: qwen/qwen2.5-vl-72b-instruct", "code": 404}})


def make(*responses, **settings):
    s = Settings(api_key="k")
    for key, value in settings.items():
        setattr(s, key, value)
    llm = LLM(s, session=Http(*responses), sleep=Clock().sleep)
    saved, notes = [], []
    llm.on_model_change = saved.append
    llm.notify = notes.append
    return llm, saved, notes


def test_auto_picks_free_qwen_with_vision():
    llm, saved, notes = make(CATALOG, ok("{}"))
    assert Settings().model == "auto"
    assert llm.chat([{"role": "user", "content": "hi"}]) == "{}"
    assert llm.model == "qwen/qwen3-vl-30b:free" and saved == ["qwen/qwen3-vl-30b:free"]
    assert llm.http.posts[0]["model"] == "qwen/qwen3-vl-30b:free" and "Автовыбор" in notes[0]


def test_auto_without_vision_still_prefers_free_qwen():
    llm, _, _ = make(models(("meta/llama:free", False, True), ("qwen/qwen-text:free", False, True)), ok(),
                     vision=False)
    llm.chat([{"role": "user", "content": "hi"}])
    assert llm.model == "qwen/qwen-text:free"


def test_vanished_free_model_is_replaced_and_saved():
    # Ровно случай из тикета #1: qwen2.5-vl-72b-instruct:free больше не бесплатная (HTTP 404).
    llm, saved, notes = make(GONE, CATALOG, ok("готово"), model="qwen/qwen2.5-vl-72b-instruct:free")
    assert llm.chat([{"role": "user", "content": "hi"}]) == "готово"
    assert saved == ["qwen/qwen3-vl-30b:free"]
    assert [p["model"] for p in llm.http.posts] == ["qwen/qwen2.5-vl-72b-instruct:free", "qwen/qwen3-vl-30b:free"]
    assert "недоступна" in notes[0]


def test_fallback_model_is_tried_first_when_model_vanishes():
    llm, saved, _ = make(GONE, ok("ok"), model="qwen/old:free", fallback_model="qwen/backup:free")
    assert llm.chat([{"role": "user", "content": "hi"}]) == "ok" and saved == ["qwen/backup:free"]


def test_no_free_models_gives_clear_error():
    llm, _, _ = make(models(("qwen/paid-vl", True, False)))
    with pytest.raises(LLMError, match="нет бесплатных"):
        llm.chat([{"role": "user", "content": "hi"}])


def test_unknown_prices_pick_qwen_vision():
    # DashScope и свои серверы не сообщают цены — тогда просто лучшая Qwen с картинками.
    r = Resp(200, {"data": [{"id": "qwen-max"}, {"id": "qwen-vl-max"}, {"id": "llama"}]})
    r.raise_for_status = lambda: None
    llm, _, _ = make(r, ok())
    llm.chat([{"role": "user", "content": "hi"}])
    assert llm.model == "qwen-vl-max"


def test_stop_interrupts_a_hanging_request_at_once():
    release = threading.Event()

    class Hanging(Http):
        def post(self, url, json=None, headers=None, timeout=None):
            release.wait(10)  # модель «думает» до 2 минут
            return ok()

    s = Settings(api_key="k", model="qwen/x")
    llm = LLM(s, session=Hanging())
    stop = threading.Event()
    llm.should_stop = stop.is_set
    threading.Timer(0.3, stop.set).start()
    start = time.monotonic()
    with pytest.raises(LLMError, match="Остановлено"):
        llm.chat([{"role": "user", "content": "hi"}])
    assert time.monotonic() - start < 2
    release.set()


def test_console_silence_saves_a_diagnostic_screenshot():
    replies = [{"summary": "", "questions": [], "plan": ["x"]}, {"action": "fail", "reason": "нет консоли"}]
    agent, _, op, _ = make_agent(replies, ["no", YES, "no"], silent=True)
    assert agent.run() is False
    warning = next(e for e in op.events if e[0] == "warning" and "не ответила" in e[1])
    assert warning[2]["image"] == "step_0.jpg" and "Передавать комбинации клавиш" in warning[1]
    assert op.asks[0][0] == "error" and "вручную" in op.asks[0][1]


def test_probe_is_retried_after_technician_opens_powershell():
    # AnyDesk не передал Win+R: техник открыл PowerShell сам и нажал «Повторить» — проба проходит.
    replies = [{"summary": "", "questions": [], "plan": ["x"]}, {"action": "done", "summary": "ok"}]
    agent, llm, op, remote = make_agent(replies, [], silent=True)
    answers = iter([YES, YES, YES])

    def ask(kind, text, **data):
        op.asks.append((kind, text, data))
        if kind == "error":
            remote.silent = False  # теперь консоль у пользователя открыта и в фокусе
        return next(answers)

    op.ask = ask
    remote.outputs = {"COMPUTERNAME": "PC: BUH-7; admin: False"}
    assert agent.run() is True
    assert remote.terminals == [False]  # Win+R второй раз не жмём — PowerShell уже открыт
    assert agent.state.probe.startswith("PC: BUH-7")
    assert [a[0] for a in op.asks] == ["error", "plan", "done"]


def test_stop_on_console_prompt_ends_session():
    agent, _, _, _ = make_agent([], ["stop"], silent=True)
    with pytest.raises(StopSession):
        agent.run()
