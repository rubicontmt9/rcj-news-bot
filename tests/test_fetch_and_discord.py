import gzip
import unittest
import urllib.error
from io import BytesIO
from unittest import mock

from rcj_news import discord
from rcj_news.fetch import FetchError, Response, decode_body, fetch, fetch_first


class DecodeBodyTest(unittest.TestCase):
    def test_utf8(self):
        self.assertEqual(decode_body("千葉".encode("utf-8")), "千葉")

    def test_shift_jis_from_header(self):
        body = "千葉ノード".encode("cp932")
        self.assertEqual(decode_body(body, "text/html; charset=Shift_JIS"), "千葉ノード")

    def test_shift_jis_from_meta_tag(self):
        body = '<meta charset="shift_jis">千葉'.encode("cp932")
        self.assertEqual(decode_body(body, "text/html"), '<meta charset="shift_jis">千葉')

    def test_euc_jp_fallback(self):
        # cp932 でも例外なく読めてしまう（文字化けする）ので、内容で選び直す
        body = "ロボカップ".encode("euc-jp")
        self.assertEqual(decode_body(body, "text/html"), "ロボカップ")

    def test_declared_charset_wins_over_guessing(self):
        body = "ロボカップジュニア".encode("cp932")
        self.assertEqual(
            decode_body(body, "text/html; charset=Shift_JIS"), "ロボカップジュニア"
        )

    def test_empty_body(self):
        self.assertEqual(decode_body(b""), "")

    def test_xml_declaration_encoding(self):
        body = '<?xml version="1.0" encoding="Shift_JIS"?><rss>サッカー</rss>'.encode("cp932")
        self.assertIn("サッカー", decode_body(body))

    def test_unknown_charset_falls_back(self):
        self.assertEqual(decode_body("ok".encode(), "text/html; charset=bogus-9"), "ok")

    def test_never_raises(self):
        self.assertIsInstance(decode_body(b"\xff\xfe\x00garbage"), str)


class ResponseTest(unittest.TestCase):
    def test_header_lookup_is_case_insensitive(self):
        response = Response("u", 200, {"ETag": 'W/"1"'}, b"")
        self.assertEqual(response.header("etag"), 'W/"1"')
        self.assertIsNone(response.header("missing"))

    def test_not_modified(self):
        self.assertTrue(Response("u", 304, {}, b"").not_modified)


class FakeHTTPResponse:
    def __init__(self, status=200, body=b"ok", headers=None, url="https://example.com/"):
        self.status = status
        self._body = BytesIO(body)
        self.headers = _Headers(headers or {})
        self._url = url

    def read(self, size=-1):
        return self._body.read(size)

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _Headers(dict):
    def items(self):
        return list(super().items())

    def get(self, key, default=None):
        for existing, value in super().items():
            if existing.lower() == key.lower():
                return value
        return default


class FetchTest(unittest.TestCase):
    def test_success(self):
        with mock.patch("urllib.request.urlopen", return_value=FakeHTTPResponse()):
            response = fetch("https://example.com/")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, b"ok")

    def test_gzip_is_decompressed(self):
        body = gzip.compress("サッカー".encode("utf-8"))
        fake = FakeHTTPResponse(body=body, headers={"Content-Encoding": "gzip"})
        with mock.patch("urllib.request.urlopen", return_value=fake):
            response = fetch("https://example.com/")
        self.assertEqual(response.text(), "サッカー")

    def test_304_returns_not_modified(self):
        error = urllib.error.HTTPError(
            "https://example.com/", 304, "Not Modified", _Headers({}), None
        )
        with mock.patch("urllib.request.urlopen", side_effect=error):
            response = fetch("https://example.com/", etag='W/"1"')
        self.assertTrue(response.not_modified)

    def test_404_is_not_retried(self):
        error = urllib.error.HTTPError(
            "https://example.com/", 404, "Not Found", _Headers({}), BytesIO(b"")
        )
        with mock.patch("urllib.request.urlopen", side_effect=error) as opener:
            with self.assertRaises(FetchError):
                fetch("https://example.com/", retries=3)
        self.assertEqual(opener.call_count, 1)

    def test_network_error_is_retried(self):
        with mock.patch("time.sleep"):
            with mock.patch(
                "urllib.request.urlopen", side_effect=urllib.error.URLError("dns")
            ) as opener:
                with self.assertRaises(FetchError):
                    fetch("https://example.com/", retries=3)
        self.assertEqual(opener.call_count, 3)

    def test_conditional_headers_are_sent(self):
        with mock.patch("urllib.request.urlopen", return_value=FakeHTTPResponse()) as opener:
            fetch("https://example.com/", etag='W/"1"', last_modified="Mon, 10 Aug 2026 00:00:00 GMT")
        request = opener.call_args[0][0]
        self.assertEqual(request.get_header("If-none-match"), 'W/"1"')
        self.assertEqual(request.get_header("If-modified-since"), "Mon, 10 Aug 2026 00:00:00 GMT")


class FetchFirstTest(unittest.TestCase):
    def test_returns_first_success(self):
        with mock.patch("rcj_news.fetch.fetch", return_value=Response("u2", 200, {}, b"ok")) as fetcher:
            response = fetch_first(["https://a", "https://b"])
        self.assertEqual(response.status, 200)
        self.assertEqual(fetcher.call_count, 1)

    def test_falls_through_to_next_candidate(self):
        responses = [FetchError("404"), Response("https://b", 200, {}, b"ok")]
        with mock.patch("rcj_news.fetch.fetch", side_effect=responses):
            response = fetch_first(["https://a", "https://b"])
        self.assertEqual(response.url, "https://b")

    def test_empty_body_is_treated_as_failure(self):
        with mock.patch("rcj_news.fetch.fetch", return_value=Response("u", 200, {}, b"")):
            with self.assertRaises(FetchError):
                fetch_first(["https://a"])

    def test_all_failed_reports_every_url(self):
        with mock.patch("rcj_news.fetch.fetch", side_effect=FetchError("boom")):
            with self.assertRaises(FetchError) as ctx:
                fetch_first(["https://a", "https://b"])
        self.assertEqual(str(ctx.exception).count("boom"), 2)


class DiscordPostTest(unittest.TestCase):
    def test_success(self):
        with mock.patch("rcj_news.discord._post_once", return_value=(204, "")):
            discord.post_message("https://discord.test/hook", {"content": "hi"})

    def test_client_error_raises_immediately(self):
        with mock.patch("rcj_news.discord._post_once", return_value=(400, '{"message":"bad"}')) as poster:
            with self.assertRaises(discord.DiscordError):
                discord.post_message("https://discord.test/hook", {"content": "hi"})
        self.assertEqual(poster.call_count, 1)

    def test_rate_limit_is_retried_after_wait(self):
        responses = [(429, '{"retry_after": 1.5}'), (204, "")]
        with mock.patch("rcj_news.discord._post_once", side_effect=responses):
            with mock.patch("time.sleep") as sleeper:
                discord.post_message("https://discord.test/hook", {"content": "hi"})
        sleeper.assert_called_once()
        self.assertAlmostEqual(sleeper.call_args[0][0], 2.0)

    def test_rate_limit_milliseconds_are_converted(self):
        responses = [(429, '{"retry_after": 3000}'), (204, "")]
        with mock.patch("rcj_news.discord._post_once", side_effect=responses):
            with mock.patch("time.sleep") as sleeper:
                discord.post_message("https://discord.test/hook", {"content": "hi"})
        self.assertLessEqual(sleeper.call_args[0][0], 4.0)

    def test_server_error_is_retried_then_fails(self):
        with mock.patch("rcj_news.discord._post_once", return_value=(500, "oops")) as poster:
            with mock.patch("time.sleep"):
                with self.assertRaises(discord.DiscordError):
                    discord.post_message("https://discord.test/hook", {"content": "hi"})
        self.assertEqual(poster.call_count, discord.MAX_ATTEMPTS)

    def test_post_messages_sends_all(self):
        with mock.patch("rcj_news.discord.post_message") as poster:
            with mock.patch("time.sleep"):
                sent = discord.post_messages("https://discord.test/hook", [{"a": 1}, {"b": 2}])
        self.assertEqual(sent, 2)
        self.assertEqual(poster.call_count, 2)

    def test_payload_is_sent_as_utf8_json(self):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["data"] = request.data
            captured["content_type"] = request.get_header("Content-type")
            return FakeHTTPResponse(status=204, body=b"")

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            discord.post_message("https://discord.test/hook", {"content": "サッカー"})
        self.assertEqual(captured["content_type"], "application/json")
        self.assertIn("サッカー", captured["data"].decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
