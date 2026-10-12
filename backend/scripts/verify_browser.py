"""End-to-end editor checks using actual FastAPI, Chromium and IndexedDB."""
import base64
from pathlib import Path
import tempfile
import time

from playwright.sync_api import sync_playwright, expect

from verify_studio import free_port, make_env, prepare_database, running_server

ARTIFACTS = Path("browser-artifacts")
EMAIL = "browser@example.com"
PASSWORD = "Browser-Test-Password-2026"
TITLE = "Проверка браузера"
CHAPTER_TITLE = "Первая встреча"
ORIGINAL = "Под вечер Мария вернулась в старый дом.\n\nНа столе её ожидало письмо, которое меняло всё."
MOBILE_EDIT = ORIGINAL + "\n\nОна решила открыть письмо после ужина."
OFFLINE_EDIT = MOBILE_EDIT + "\n\nЧерновик без сети: дверь в сад оставалась открытой."


def login(page, origin):
    page.goto(origin, wait_until="networkidle")
    page.get_by_test_id("login-email").fill(EMAIL)
    page.get_by_test_id("login-password").fill(PASSWORD)
    page.get_by_test_id("login-submit").click()
    expect(page.get_by_role("button", name="＋ Новый роман")).to_be_visible()


def open_chapter(page, novel_id, chapter_id):
    page.get_by_test_id(f"novel-open-{novel_id}").click()
    page.get_by_test_id(f"chapter-open-{chapter_id}").click()
    expect(page.get_by_test_id("chapter-editor")).to_be_visible()


def wait_saved(page, origin, chapter_id, text):
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        response = page.request.get(f"{origin}/api/v1/chapters/{chapter_id}")
        if response.ok and response.json()["current_revision"]["text"] == text:
            return
        time.sleep(0.2)
    raise AssertionError("Автосохранение не доставило текущий текст на сервер")


def main():
    ARTIFACTS.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="novel-browser-") as temporary:
        port = free_port()
        origin = f"http://127.0.0.1:{port}"
        env = make_env(Path(temporary) / "studio.db", origin)
        prepare_database(env, EMAIL, PASSWORD)
        with running_server(env, port), sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            desktop = browser.new_context(viewport={"width": 1440, "height": 1000})
            page = desktop.new_page()
            page.on("dialog", lambda dialog: dialog.accept())
            try:
                login(page, origin)
                page.get_by_role("button", name="＋ Новый роман").click()
                page.get_by_test_id("novel-create-title").fill(TITLE)
                page.get_by_test_id("novel-create-submit").click()
                page.get_by_test_id("chapter-create-title").fill(CHAPTER_TITLE)
                page.get_by_test_id("chapter-create-submit").click()
                editor = page.get_by_test_id("chapter-editor")
                expect(editor).to_be_editable()
                editor.fill(ORIGINAL)
                novels = page.request.get(origin + "/api/v1/novels").json()["items"]
                novel_id = next(n["id"] for n in novels if n["title"] == TITLE)
                chapters = page.request.get(f"{origin}/api/v1/novels/{novel_id}/chapters").json()["items"]
                chapter_id = next(c["id"] for c in chapters if c["title"] == CHAPTER_TITLE)
                wait_saved(page, origin, chapter_id, ORIGINAL)
                preview = page.screenshot(path=str(ARTIFACTS / "desktop-editor.png"), full_page=True)
                print("DESKTOP_PREVIEW_BASE64=" + base64.b64encode(preview).decode())

                mobile = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
                phone = mobile.new_page()
                login(phone, origin)
                open_chapter(phone, novel_id, chapter_id)
                expect(phone.get_by_test_id("chapter-editor")).to_have_value(ORIGINAL)
                expect(phone.get_by_test_id("chapter-editor")).to_be_editable()
                phone.get_by_test_id("chapter-editor").fill(MOBILE_EDIT)
                wait_saved(phone, origin, chapter_id, MOBILE_EDIT)
                preview = phone.screenshot(path=str(ARTIFACTS / "mobile-editor.png"), full_page=True)
                print("MOBILE_PREVIEW_BASE64=" + base64.b64encode(preview).decode())
                assert phone.evaluate("document.body.scrollWidth <= window.innerWidth + 1"), "Mobile layout overflows horizontally"
                mobile.close()

                page.reload(wait_until="networkidle")
                open_chapter(page, novel_id, chapter_id)
                expect(page.get_by_test_id("chapter-editor")).to_have_value(MOBILE_EDIT)
                # A cached shell and owner-scoped IndexedDB must survive an offline reload.
                page.evaluate("() => navigator.serviceWorker.ready.then(() => true)")
                page.wait_for_function("() => navigator.serviceWorker.controller !== null")
                desktop.set_offline(True)
                page.get_by_test_id("chapter-editor").fill(OFFLINE_EDIT)
                page.wait_for_timeout(1200)
                page.reload(wait_until="domcontentloaded")
                open_chapter(page, novel_id, chapter_id)
                expect(page.get_by_test_id("chapter-editor")).to_have_value(OFFLINE_EDIT)
                page.screenshot(path=str(ARTIFACTS / "offline-draft.png"), full_page=True)
                desktop.set_offline(False)
                wait_saved(page, origin, chapter_id, OFFLINE_EDIT)
                # Same-profile tabs share IndexedDB and must respect the chapter lock.
                second = desktop.new_page()
                second.goto(origin, wait_until="networkidle")
                open_chapter(second, novel_id, chapter_id)
                expect(second.get_by_test_id("chapter-editor")).not_to_be_editable()
                page.get_by_role("button", name="Библиотека").click()
                second.reload(wait_until="networkidle")
                open_chapter(second, novel_id, chapter_id)
                expect(second.get_by_test_id("chapter-editor")).to_be_editable()
                second.close()

                # Cached pagination must preserve opened chapters beyond the first 100.
                session = page.request.get(origin + "/api/v1/session").json()
                headers = {"Origin": origin, "X-CSRF-Token": session["csrf_token"]}
                late_id = ""
                for number in range(2, 102):
                    result = page.request.post(
                        f"{origin}/api/v1/novels/{novel_id}/chapters",
                        data={"number": number, "title": f"Глава {number}"}, headers=headers)
                    assert result.status == 201, result.text()
                    late_id = result.json()["id"]
                page.get_by_test_id(f"novel-open-{novel_id}").click()
                page.get_by_role("button", name="Загрузить ещё", exact=True).click()
                page.get_by_test_id(f"chapter-open-{late_id}").click()
                later_text = "Глава за пределами первой страницы: авторский текст сохранён."
                expect(page.get_by_test_id("chapter-editor")).to_be_editable()
                page.get_by_test_id("chapter-editor").fill(later_text)
                wait_saved(page, origin, late_id, later_text)
                desktop.set_offline(True)
                page.reload(wait_until="domcontentloaded")
                page.get_by_test_id(f"novel-open-{novel_id}").click()
                expect(page.get_by_test_id(f"chapter-open-{late_id}")).to_be_visible()
                expect(page.get_by_test_id("chapter-editor")).to_have_value(later_text)
                late_offline = later_text + "\\nПравка главы 101 без сети."
                page.get_by_test_id("chapter-editor").fill(late_offline)
                page.wait_for_timeout(1200)
                page.reload(wait_until="domcontentloaded")
                page.get_by_test_id(f"novel-open-{novel_id}").click()
                expect(page.get_by_test_id("chapter-editor")).to_have_value(late_offline)
                desktop.set_offline(False)
                wait_saved(page, origin, late_id, late_offline)
                print("Browser passed: desktop/mobile sync, offline reload/reconnect, two-tab locks and chapter 101 offline.")
            except Exception:
                page.screenshot(path=str(ARTIFACTS / "failure.png"), full_page=True)
                raise
            finally:
                desktop.close()
                browser.close()


if __name__ == "__main__":
    main()
