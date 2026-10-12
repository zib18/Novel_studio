"""End-to-end editor checks using actual FastAPI, Chromium and IndexedDB."""
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
                page.screenshot(path=str(ARTIFACTS / "desktop-editor.png"), full_page=True)

                mobile = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
                phone = mobile.new_page()
                login(phone, origin)
                open_chapter(phone, novel_id, chapter_id)
                expect(phone.get_by_test_id("chapter-editor")).to_have_value(ORIGINAL)
                expect(phone.get_by_test_id("chapter-editor")).to_be_editable()
                phone.get_by_test_id("chapter-editor").fill(MOBILE_EDIT)
                wait_saved(phone, origin, chapter_id, MOBILE_EDIT)
                phone.screenshot(path=str(ARTIFACTS / "mobile-editor.png"), full_page=True)
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
                print("Browser passed: desktop edit, mobile synchronization, offline reload and reconnect.")
            except Exception:
                page.screenshot(path=str(ARTIFACTS / "failure.png"), full_page=True)
                raise
            finally:
                desktop.close()
                browser.close()


if __name__ == "__main__":
    main()
