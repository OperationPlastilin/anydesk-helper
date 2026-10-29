import pytest
from PIL import Image

from anydesk_helper import screen
from anydesk_helper.actions import (ActionError, extract_json, needs_confirmation,
                                    normalize_keys, parse_action)
from anydesk_helper.prompts import load_playbooks, plan_system, step_system
from anydesk_helper.screen import Frame, Region


def test_extract_json_from_noisy_reply():
    reply = '<think>hmm {not json}</think>Sure!\n```json\n{"action": "wait", "seconds": 3}\n```'
    assert extract_json(reply) == {"action": "wait", "seconds": 3}


def test_extract_json_fails_without_object():
    with pytest.raises(ActionError):
        extract_json("I will click the button")


def test_parse_run_with_strict_format():
    a = parse_action({"observation": "консоль открыта", "plan_step": "2", "thought": "проверю",
                      "action": "run", "command": "Get-Printer", "timeout": 5000, "risky": False})
    assert (a.type, a.command, a.plan_step, a.timeout) == ("run", "Get-Printer", 2, 1800)
    assert a.observation == "консоль открыта"


def test_parse_aliases():
    assert parse_action({"action": "ask_user", "question": "Ключ?"}).type == "ask_operator"
    assert parse_action({"action": "powershell", "command": "dir"}).type == "run"


def test_click_requires_screenshot_in_this_step():
    with pytest.raises(ActionError, match="screenshot"):
        parse_action({"action": "click", "x": 10, "y": 10})
    a = parse_action({"action": "click", "bbox_2d": [10, 20, 30, 40]}, 800, 600)
    assert (a.x, a.y) == (20, 30)
    b = parse_action({"action": "double_click", "x": 500, "y": 250}, 800, 600, "norm1000")
    assert (b.x, b.y) == (400, 150)


def test_parse_rejects_bad_actions():
    with pytest.raises(ActionError):
        parse_action({"action": "explode"})
    with pytest.raises(ActionError):
        parse_action({"action": "run", "command": ""})
    with pytest.raises(ActionError):
        parse_action({"action": "click", "x": 900, "y": 10}, 800, 600)
    with pytest.raises(ActionError):
        parse_action({"action": "key", "keys": ["ctrl", "banana"]})


def test_normalize_keys():
    assert normalize_keys("Ctrl+Shift+Esc") == ["ctrl", "shift", "esc"]
    assert normalize_keys(["Windows", "R"]) == ["win", "r"]


def test_confirmation_rules():
    run = lambda cmd, **kw: parse_action({"action": "run", "command": cmd, **kw})
    assert needs_confirmation(run("Remove-Item C:\\x -Recurse"), "risky")
    assert needs_confirmation(run("winget install Microsoft.Office"), "risky")
    assert needs_confirmation(run("Get-Printer", risky=True), "risky")
    assert not needs_confirmation(run("Get-Printer"), "risky")
    assert needs_confirmation(run("Get-Printer"), "all")
    assert not needs_confirmation(run("format c:"), "none")
    assert needs_confirmation(parse_action({"action": "open_terminal", "admin": True}), "risky")
    assert not needs_confirmation(parse_action({"action": "screenshot"}), "all")


def test_fit_size_and_mapping():
    w, h = screen.fit_size(1920, 1080, 1288)
    assert w % 28 == 0 and h % 28 == 0 and max(w, h) <= 1288
    frame = Frame(Image.new("RGB", (640, 360)), Region(100, 50, 1280, 720))
    assert frame.to_screen(320, 180) == (740, 410)
    assert frame.to_screen(640, 360) == (1379, 769)


def test_playbooks_selected_by_keywords(tmp_path):
    (tmp_path / "all.md").write_text("keywords: *\nобщие советы", encoding="utf-8")
    (tmp_path / "office.md").write_text("keywords: office, офис, word\nпро офис", encoding="utf-8")
    assert "про офис" in load_playbooks([tmp_path], "Установить Офис пользователю")
    text = load_playbooks([tmp_path], "сбросить password")
    assert "общие советы" in text and "про офис" not in text


def test_prompts_describe_strict_format():
    s = step_system("Russian", True, "pixels", "")
    for key in ('"observation"', '"plan_step"', '"thought"', '"action"', '"risky"', "screenshot"):
        assert key in s
    assert "- screenshot" not in step_system("Russian", False, "pixels", "")
    assert '"questions"' in plan_system("Russian", "") and '"plan"' in plan_system("Russian", "")
