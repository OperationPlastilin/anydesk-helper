import hashlib
from pathlib import Path

import pytest

from anydesk_helper import updater


class Resp:
    def __init__(self, status=200, data=None, content=b"", text=""):
        self.status_code, self._data, self.content, self.text = status, data, content, text

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def iter_content(self, n):
        yield self.content

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class Http:
    def __init__(self, routes):
        self.routes = routes

    def get(self, url, **kw):
        return self.routes[url]


RELEASE = {"tag_name": "v1.4.0", "body": "Новое", "html_url": "https://x",
           "assets": [{"name": "AnyDeskHelper.exe", "browser_download_url": "https://dl/exe"},
                      {"name": "AnyDeskHelper.exe.sha256", "browser_download_url": "https://dl/sha"}]}


def test_parse_version():
    assert updater.parse_version("v1.2.3") == (1, 2, 3)
    assert updater.parse_version("0.10") == (0, 10, 0)
    assert updater.parse_version("v2.0.0-beta") == (2, 0, 0)
    assert updater.parse_version("v0.10.0") > updater.parse_version("v0.9.9")


def test_check_finds_newer_release():
    http = Http({updater.API_URL: Resp(data=RELEASE)})
    info = updater.check("1.3.9", http=http)
    assert info["version"] == "1.4.0" and info["url"] == "https://dl/exe"
    assert updater.check("1.4.0", http=http) is None
    assert updater.check("1.0", http=Http({updater.API_URL: Resp(404)})) is None


def test_download_verifies_checksum(tmp_path):
    blob = b"MZ new version"
    good = hashlib.sha256(blob).hexdigest()
    info = {"url": "https://dl/exe", "sha256_url": "https://dl/sha"}
    http = Http({"https://dl/exe": Resp(content=blob), "https://dl/sha": Resp(text=f"{good}  AnyDeskHelper.exe")})
    updater.download(info, tmp_path / "new.exe", http=http)
    assert (tmp_path / "new.exe").read_bytes() == blob

    http.routes["https://dl/sha"] = Resp(text="0" * 64)
    with pytest.raises(updater.UpdateError):
        updater.download(info, tmp_path / "bad.exe", http=http)
    assert not (tmp_path / "bad.exe").exists()


def test_install_refuses_when_not_frozen():
    with pytest.raises(updater.UpdateError):
        updater.install({"url": "x"})


def test_check_bad_response_is_update_error():
    class NotJson(Resp):
        def json(self):
            raise ValueError("Expecting value: line 1 column 1")

    for resp in (NotJson(), Resp(data=[1, 2]), Resp(data={"tag_name": "v9.0.0", "assets": [{"x": 1}]})):
        with pytest.raises(updater.UpdateError):
            updater.check("1.0.0", http=Http({updater.API_URL: resp}))


def test_download_removes_partial_file(tmp_path):
    class Broken(Resp):
        def iter_content(self, n):
            yield b"MZ half"
            raise OSError(28, "No space left on device")

    info = {"url": "https://dl/exe"}
    with pytest.raises(OSError):
        updater.download(info, tmp_path / "new.exe", http=Http({"https://dl/exe": Broken()}))
    assert list(tmp_path.iterdir()) == []
    http = Http({"https://dl/exe": Resp(content=b"MZ"), "https://dl/sha": Resp(text="")})
    with pytest.raises(updater.UpdateError):  # пустой .sha256 — не повод пропустить проверку
        updater.download({**info, "sha256_url": "https://dl/sha"}, tmp_path / "new.exe", http=http)


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    """Как в собранном exe: путь с кириллицей, переменные onefile-процесса PyInstaller."""
    exe = tmp_path / "Иван" / "AnyDeskHelper.exe"
    exe.parent.mkdir()
    monkeypatch.setattr(updater, "can_self_update", lambda: True)
    monkeypatch.setattr(updater.sys, "executable", str(exe))
    monkeypatch.setattr(updater.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setenv("_PYI_ARCHIVE_FILE", str(exe))
    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", str(tmp_path / "_MEI12345"))
    monkeypatch.setenv("_PYI_PARENT_PROCESS_LEVEL", "1")
    monkeypatch.setenv("_MEIPASS2", str(tmp_path / "_MEI12345"))
    for name, value in (("CREATE_NO_WINDOW", 0x08000000), ("DETACHED_PROCESS", 0x8),
                        ("CREATE_NEW_PROCESS_GROUP", 0x200)):
        monkeypatch.setattr(updater.subprocess, name, value, raising=False)
    popen = []
    monkeypatch.setattr(updater.subprocess, "Popen", lambda cmd, **kw: popen.append((cmd, kw)))
    return exe, popen


def test_install_restarts_cleanly(frozen, tmp_path, monkeypatch):
    exe, popen = frozen
    monkeypatch.setattr(updater, "download", lambda info, dest: dest.write_bytes(b"MZ"))
    updater.install({"url": "x"}, restart_args=["--port", "9000"], exit_after=None)
    (cmd, kw), = popen
    assert cmd[:3] == ["cmd", "/d", "/c"]
    text = Path(cmd[3]).read_bytes().decode("ascii")  # .bat — чистый ASCII, без путей пользователя
    assert "chcp" not in text and "%AIH_EXE%" in text and str(tmp_path) not in text
    env = kw["env"]
    assert env["AIH_EXE"] == str(exe) and env["AIH_NEW"] == str(exe.with_name("AnyDeskHelper.new.exe"))
    assert env["AIH_ARGS"] == "--port 9000 --no-update"  # новая версия не обновляется сразу снова
    # Новый exe — независимый процесс: распаковывается заново, а не в удалённую папку старого.
    assert env["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert not [k for k in env if k.startswith(("_PYI_", "_MEIPASS"))]
    assert kw["creationflags"] & 0x08000000 and not kw["creationflags"] & 0x8  # без DETACHED_PROCESS


def test_install_turns_os_errors_into_update_error(frozen, monkeypatch):
    exe, popen = frozen

    def denied(info, dest):
        raise PermissionError(13, "Отказано в доступе", str(dest))

    monkeypatch.setattr(updater, "download", denied)
    with pytest.raises(updater.UpdateError, match="права на запись"):
        updater.install({"url": "x"}, exit_after=None)
    assert popen == []


def test_startup_update_never_blocks_launch(monkeypatch, tmp_path, capsys):
    from anydesk_helper import __main__ as main_mod
    from anydesk_helper.config import SettingsStore

    store = SettingsStore(tmp_path / "settings.json")
    monkeypatch.setattr(updater, "can_self_update", lambda: True)
    monkeypatch.setattr(updater, "check", lambda current: {"version": "9.9.9", "url": "x"})

    def install(info, restart_args=None, exit_after=1.0):
        raise PermissionError(13, "Отказано в доступе")

    monkeypatch.setattr(updater, "install", install)
    assert main_mod.startup_update(store, []) is False
    assert "Обновление пропущено" in capsys.readouterr().out
