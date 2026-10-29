import hashlib

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
