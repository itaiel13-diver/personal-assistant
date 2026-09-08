import ast
import inspect
from unittest.mock import MagicMock, patch

import drive_tools

FOLDER = "folder-id-1"


def _service(files_mock):
    """A stand-in for the built Drive client, where service.files() is ours."""
    service = MagicMock()
    service.files.return_value = files_mock
    return service


def _files(**returns):
    """A files() resource whose named methods return the given payloads.

    Written this way because every call in drive_tools is the same shape -
    files().something(...).execute() - so the mock only has to answer .execute().
    """
    files = MagicMock()
    for name, payload in returns.items():
        getattr(files, name).return_value.execute.return_value = payload
    return files


# --- The three rules the module docstring promises. These are the guard. ---

def _calls(tree):
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def _tree():
    return ast.parse(open(drive_tools.__file__, encoding="utf-8").read())


def test_deleting_a_file_means_the_bin_unless_asked_otherwise():
    """This module used to have no way to remove a file at all. Itai lifted that
    on 2026-09-07 - they are his files, and he would rather have the verb and
    narrow it later than keep hitting a wall. What is guarded now is the
    default: an ordinary call bins the file, which Drive keeps recoverable for
    30 days, and the irreversible delete has to be asked for by name."""
    signature = inspect.signature(drive_tools.trash_drive_file)
    assert signature.parameters["permanent"].default is False, (
        "trash_drive_file now destroys files unless told not to"
    )

    files = _files(
        get={"name": "דוח ספטמבר", "ownedByMe": True, "trashed": False},
        update={"id": "f1"},
    )
    with patch.object(drive_tools, "_drive_service", return_value=_service(files)):
        out = drive_tools.trash_drive_file("f1")

    files.delete.assert_not_called()
    assert files.update.call_args.kwargs["body"] == {"trashed": True}
    assert "דוח ספטמבר" in out and "30 יום" in out


def test_a_permanent_delete_happens_only_when_it_is_asked_for():
    """The one call in the module with no undo. It must reach files().delete()
    when asked - a 'permanent' flag that quietly still bins would be worse than
    no flag - and must say plainly that nothing can be recovered."""
    files = _files(get={"name": "טיוטה ישנה", "ownedByMe": True, "trashed": False})
    with patch.object(drive_tools, "_drive_service", return_value=_service(files)):
        out = drive_tools.trash_drive_file("f9", permanent=True)

    files.delete.assert_called_once()
    assert files.delete.call_args.kwargs["fileId"] == "f9"
    files.update.assert_not_called()
    assert "לצמיתות" in out


def test_binning_something_already_in_the_bin_does_not_call_google_again():
    """Cheap, but it is the difference between a truthful answer and a second
    confirmation that implies work happened twice."""
    files = _files(get={"name": "כבר בפח", "ownedByMe": True, "trashed": True})
    with patch.object(drive_tools, "_drive_service", return_value=_service(files)):
        out = drive_tools.trash_drive_file("f2")

    files.update.assert_not_called()
    files.delete.assert_not_called()
    assert "כבר" in out


def test_module_can_never_change_who_can_see_a_file():
    """Sharing is the other irreversible act: a document made public cannot be
    made private again for whoever already copied it. The assistant has no
    reason to touch permissions and now no way to."""
    called = _calls(_tree())
    assert "permissions" not in called, "drive_tools now touches permissions()"
    attributes = {
        node.attr
        for node in ast.walk(_tree())
        if isinstance(node, ast.Attribute)
    }
    assert "permissions" not in attributes


def test_every_new_file_is_created_inside_the_working_folder():
    """A create() without parents lands in the root of Itai's Drive, which is
    exactly the mess this design exists to avoid. Checked structurally so a new
    creation path cannot forget it."""
    tree = _tree()
    creates = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "create"
    ]
    assert creates, "no files().create() found - has the module been restructured?"
    for call in creates:
        body = next((kw.value for kw in call.keywords if kw.arg == "body"), None)
        assert body is not None, "a create() call passes no body"
        source = ast.dump(body)
        assert "FOLDER_ID" in source, "a create() call does not put the file in FOLDER_ID"


def test_scopes_are_the_ones_the_refresh_token_script_asks_for():
    """One consent mints one token, and each new consent supersedes the last. If
    the list this module presents and the list the script asks for drift apart,
    nothing fails at import - it fails in production as an opaque 403 on a call
    that worked yesterday.

    This used to be checked by grepping the script's source for each scope
    string, which was true only for as long as somebody kept three copies of the
    list in step by hand. There is one list now, in google_scopes, and both ends
    are the same object - so the drift this test was written for cannot happen
    rather than being detected after the fact. What is left to check is that
    neither end has quietly gone back to a private copy."""
    import google_scopes
    import scripts.get_gmail_refresh_token as mint

    assert drive_tools.SCOPES is google_scopes.SCOPES
    assert mint.SCOPES is google_scopes.SCOPES
    assert "https://www.googleapis.com/auth/drive" in drive_tools.SCOPES


# --- Searching ---

def test_search_covers_files_shared_with_him_by_default():
    """The half of the request he asked for by name. A search that quietly
    limited itself to his own files would look like it worked."""
    files = _files(list={"files": []})
    with patch.object(drive_tools, "_drive_service", return_value=_service(files)):
        drive_tools.search_drive("תוכנית עבודה")
    query = files.list.call_args.kwargs["q"]
    assert "sharedWithMe" not in query, "the default search excludes shared files"
    assert "trashed = false" in query


def test_search_can_be_narrowed_to_shared_files_only():
    files = _files(list={"files": []})
    with patch.object(drive_tools, "_drive_service", return_value=_service(files)):
        drive_tools.search_drive("מצגת", shared_with_me_only=True)
    assert "sharedWithMe = true" in files.list.call_args.kwargs["q"]


def test_search_looks_inside_files_and_not_only_at_names():
    files = _files(list={"files": []})
    with patch.object(drive_tools, "_drive_service", return_value=_service(files)):
        drive_tools.search_drive("קריית עקרון")
    query = files.list.call_args.kwargs["q"]
    assert "fullText contains" in query and "name contains" in query


def test_an_apostrophe_in_the_search_does_not_break_the_query():
    """Drive query literals are single-quoted. Unescaped, O'Brien ends the
    string early and Google answers with a syntax error instead of results."""
    files = _files(list={"files": []})
    with patch.object(drive_tools, "_drive_service", return_value=_service(files)):
        drive_tools.search_drive("O'Brien")
    assert "\\'" in files.list.call_args.kwargs["q"]


def test_search_results_carry_the_id_the_other_tools_need():
    files = _files(list={"files": [
        {"id": "abc123", "name": "דוח ספטמבר", "mimeType": "application/vnd.google-apps.document",
         "modifiedTime": "2026-09-01T10:00:00Z", "owners": [{"displayName": "ניקיטה"}],
         "shared": True, "ownedByMe": False},
    ]})
    with patch.object(drive_tools, "_drive_service", return_value=_service(files)):
        out = drive_tools.search_drive("דוח")
    assert "[id:abc123]" in out
    assert "דוח ספטמבר" in out
    assert "משותף איתך" in out


def test_search_with_nothing_to_search_for_does_not_call_google():
    files = _files(list={"files": []})
    with patch.object(drive_tools, "_drive_service", return_value=_service(files)):
        out = drive_tools.search_drive("   ")
    files.list.assert_not_called()
    assert "צריך" in out


def test_a_failed_search_explains_itself_instead_of_raising():
    """Every tool here returns a string to the model. An exception escaping into
    Gemini's function-calling loop is what produced 'תקלה בחיבור ל-AI' before."""
    with patch.object(drive_tools, "_drive_service", side_effect=RuntimeError("no token")):
        out = drive_tools.search_drive("משהו")
    assert out.startswith("❌")
    assert "no token" in out


# --- Reading ---

def test_a_google_doc_is_exported_because_it_has_no_bytes_to_download():
    files = _files(
        get={"id": "d1", "name": "סיכום", "mimeType": "application/vnd.google-apps.document"},
        export=b"tekst",
    )
    with patch.object(drive_tools, "_drive_service", return_value=_service(files)):
        with patch.object(drive_tools.attachment_readers, "extract_text", return_value="סיכום הפגישה") as extract:
            out = drive_tools.read_drive_file("d1")
    files.export.assert_called_once()
    files.get_media.assert_not_called()
    assert extract.call_args.args[0].endswith(".txt")
    assert out == "סיכום הפגישה"


def test_a_spreadsheet_is_exported_as_xlsx_so_every_tab_survives():
    """Drive's csv export carries only the FIRST tab of a spreadsheet - a
    workbook with several sheets lost all but one. xlsx keeps every tab, and
    attachment_readers walks every sheet in it."""
    files = _files(
        get={"id": "s1", "name": "Z8 Training Status", "mimeType": "application/vnd.google-apps.spreadsheet"},
        export=b"PK\x03\x04",
    )
    with patch.object(drive_tools, "_drive_service", return_value=_service(files)):
        with patch.object(drive_tools.attachment_readers, "extract_text", return_value="a,b") as extract:
            drive_tools.read_drive_file("s1")
    assert files.export.call_args.kwargs["mimeType"] == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert extract.call_args.args[0].endswith(".xlsx")


def test_a_share_link_from_an_email_is_read_by_the_id_inside_it():
    """Share notifications arrive as mail carrying a docs.google.com link.
    Opening that link in a browser hits a login wall; the id inside it is what
    the API needs."""
    real_id = "1AbCDefGhIJkLmNoPqRsTuVwXyZ0123456789"
    files = _files(
        get={"id": real_id, "name": "סיכום", "mimeType": "application/vnd.google-apps.document"},
        export=b"tekst",
    )
    with patch.object(drive_tools, "_drive_service", return_value=_service(files)):
        with patch.object(drive_tools.attachment_readers, "extract_text", return_value="x"):
            drive_tools.read_drive_file(
                f"https://docs.google.com/document/d/{real_id}/edit?usp=sharing")
    assert files.get.call_args.kwargs["fileId"] == real_id


def test_search_covers_shared_drives_and_not_only_his_own():
    """The default corpora covers his My Drive and direct shares. A file living
    in a shared drive is invisible until the search is widened to allDrives."""
    files = _files(list={"files": []})
    with patch.object(drive_tools, "_drive_service", return_value=_service(files)):
        drive_tools.search_drive("דוח")
    assert files.list.call_args.kwargs["corpora"] == "allDrives"


def test_an_uploaded_file_is_downloaded_rather_than_exported():
    files = _files(
        get={"id": "x1", "name": "נתונים.xlsx", "size": "2048",
             "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
        get_media=b"PK\x03\x04",
    )
    with patch.object(drive_tools, "_drive_service", return_value=_service(files)):
        with patch.object(drive_tools.attachment_readers, "extract_text", return_value="שורות") as extract:
            drive_tools.read_drive_file("x1")
    files.get_media.assert_called_once()
    files.export.assert_not_called()
    assert extract.call_args.args[0] == "נתונים.xlsx"


def test_a_file_too_large_to_read_says_so_before_downloading_it():
    """The size check has to happen on the metadata. Downloading 80MB into a
    512MB container to then refuse it is how the free tier gets killed."""
    files = _files(
        get={"id": "big", "name": "ענק.pdf", "size": str(200 * 1024 * 1024), "mimeType": "application/pdf"},
    )
    with patch.object(drive_tools, "_drive_service", return_value=_service(files)):
        out = drive_tools.read_drive_file("big")
    files.get_media.assert_not_called()
    assert "גדול מדי" in out


def test_reading_a_folder_says_it_is_a_folder():
    files = _files(get={"id": "f1", "name": "עבודה", "mimeType": drive_tools.FOLDER_MIME})
    with patch.object(drive_tools, "_drive_service", return_value=_service(files)):
        out = drive_tools.read_drive_file("f1")
    assert "תיקייה" in out


def test_the_part_number_is_passed_through_so_long_files_can_be_finished():
    """read_email_attachment already paginates and the prompt tells the model to
    keep going to the last part. A Drive file has to behave the same way or the
    model will believe it read a file it only saw the first page of."""
    files = _files(
        get={"id": "d1", "name": "ארוך", "mimeType": "application/vnd.google-apps.document"},
        export=b"x",
    )
    with patch.object(drive_tools, "_drive_service", return_value=_service(files)):
        with patch.object(drive_tools.attachment_readers, "extract_text", return_value="חלק 2") as extract:
            drive_tools.read_drive_file("d1", part=2)
    assert extract.call_args.kwargs["part"] == 2


# --- Writing, and the folder that bounds it ---

def test_a_new_file_goes_into_the_working_folder():
    files = _files(create={"id": "new1", "name": "סיכום", "webViewLink": "https://drive.google.com/x"})
    with patch.object(drive_tools, "FOLDER_ID", FOLDER):
        with patch.object(drive_tools, "_drive_service", return_value=_service(files)):
            out = drive_tools.create_drive_file("סיכום", "תוכן")
    assert files.create.call_args.kwargs["body"]["parents"] == [FOLDER]
    assert "[id:new1]" in out


def test_a_sheet_is_created_as_a_real_google_sheet_from_csv():
    files = _files(create={"id": "s2", "name": "טבלה"})
    with patch.object(drive_tools, "FOLDER_ID", FOLDER):
        with patch.object(drive_tools, "_drive_service", return_value=_service(files)):
            drive_tools.create_drive_file("טבלה", "עיר,סטטוס\nלוד,בוצע", file_type="sheet")
    assert files.create.call_args.kwargs["body"]["mimeType"] == "application/vnd.google-apps.spreadsheet"


def test_an_unknown_file_type_is_refused_rather_than_guessed():
    files = _files(create={"id": "no"})
    with patch.object(drive_tools, "FOLDER_ID", FOLDER):
        with patch.object(drive_tools, "_drive_service", return_value=_service(files)):
            out = drive_tools.create_drive_file("קובץ", "תוכן", file_type="pdf")
    files.create.assert_not_called()
    assert out.startswith("❌")


def test_without_a_configured_folder_nothing_is_written_anywhere():
    """An unset DRIVE_FOLDER_ID means 'nowhere', never 'anywhere'. If it meant
    'the root of his Drive' a missing environment variable would quietly turn
    into files scattered across it."""
    files = _files(create={"id": "no"})
    with patch.object(drive_tools, "FOLDER_ID", ""):
        with patch.object(drive_tools, "_drive_service", return_value=_service(files)):
            created = drive_tools.create_drive_file("קובץ", "תוכן")
            updated = drive_tools.update_drive_file("some-id", "תוכן")
    files.create.assert_not_called()
    files.update.assert_not_called()
    assert created.startswith("❌") and updated.startswith("❌")


def test_a_file_outside_the_working_folder_is_read_only():
    """The point of the whole design: a spreadsheet a colleague shared can be
    read and must not be overwritten by an assistant that misunderstood."""
    files = _files(get={"parents": ["someone-elses-folder"]})
    with patch.object(drive_tools, "FOLDER_ID", FOLDER):
        with patch.object(drive_tools, "_drive_service", return_value=_service(files)):
            out = drive_tools.update_drive_file("shared-file", "תוכן חדש")
    files.update.assert_not_called()
    assert out.startswith("❌")
    assert "עותק" in out, "the refusal should offer the copy that would work"


def test_a_file_inside_the_working_folder_can_be_edited():
    files = MagicMock()
    files.get.return_value.execute.side_effect = [
        {"parents": [FOLDER]},
        {"mimeType": "application/vnd.google-apps.document", "name": "סיכום"},
    ]
    files.update.return_value.execute.return_value = {"id": "mine", "name": "סיכום"}
    with patch.object(drive_tools, "FOLDER_ID", FOLDER):
        with patch.object(drive_tools, "_drive_service", return_value=_service(files)):
            out = drive_tools.update_drive_file("mine", "תוכן חדש")
    files.update.assert_called_once()
    assert out.startswith("✅")


def test_the_working_folder_listing_is_scoped_to_that_folder():
    files = _files(list={"files": []})
    with patch.object(drive_tools, "FOLDER_ID", FOLDER):
        with patch.object(drive_tools, "_drive_service", return_value=_service(files)):
            drive_tools.list_drive_folder()
    assert f"'{FOLDER}' in parents" in files.list.call_args.kwargs["q"]


def test_a_long_listing_is_truncated_instead_of_flooding_whatsapp():
    files = _files(list={"files": [
        {"id": f"id{i}", "name": "קובץ עם שם ארוך מאוד " * 12, "mimeType": "text/plain"}
        for i in range(15)
    ]})
    with patch.object(drive_tools, "_drive_service", return_value=_service(files)):
        out = drive_tools.search_drive("קובץ")
    assert len(out) <= drive_tools.MAX_LISTING_CHARS + 40
    assert "קוצרה" in out


# --- the bot's own identity: files shared with the service account -----------
#
# Itai shares files with the bot's address (the calendar service account) the
# way he shares with a person. read_drive_file tries as Itai first, then as
# the bot, and a double miss names the addresses that work.

from googleapiclient.errors import HttpError


def _missing(status=404):
    resp = MagicMock(status=status, reason="Not Found")
    return HttpError(resp, b"{}")


SA_JSON = '{"client_email": "calendar-bot@proj.iam.gserviceaccount.com"}'


def test_a_file_shared_with_the_bot_is_read_on_the_second_identity(monkeypatch):
    """The user token says 404, the bot's identity opens the file. This is the
    whole feature: 'share with the bot's email' must just work."""
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", SA_JSON)
    user_files = MagicMock()
    user_files.get.return_value.execute.side_effect = _missing()
    bot_files = _files(
        get={"id": "f1", "name": "מחירון", "mimeType": "application/vnd.google-apps.document"},
        export=b"tekst",
    )
    with patch.object(drive_tools, "_drive_service", return_value=_service(user_files)), \
         patch.object(drive_tools, "_sa_drive_service", return_value=_service(bot_files)), \
         patch.object(drive_tools.attachment_readers, "extract_text", return_value="טקסט המחירון"):
        out = drive_tools.read_drive_file("f1")
    assert out == "טקסט המחירון"
    bot_files.export.assert_called_once()


def test_a_file_visible_to_neither_identity_gets_the_sharing_hint(monkeypatch):
    """A bare 'not found' taught him nothing. The double miss must name the two
    addresses that work - and they come from the live key, not from memory."""
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", SA_JSON)
    dead = MagicMock()
    dead.get.return_value.execute.side_effect = _missing()
    with patch.object(drive_tools, "_drive_service", return_value=_service(dead)), \
         patch.object(drive_tools, "_sa_drive_service", return_value=_service(dead)):
        out = drive_tools.read_drive_file("f-gone")
    assert "calendar-bot@proj.iam.gserviceaccount.com" in out
    assert "itai.samsung.isr@gmail.com" in out
    assert "כל מי שיש לו קישור" in out


def test_without_an_sa_key_the_hint_names_only_his_account(monkeypatch):
    """No service-account key means no second identity to try and no address to
    invent - the hint falls back to his account alone."""
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    dead = MagicMock()
    dead.get.return_value.execute.side_effect = _missing()
    with patch.object(drive_tools, "_drive_service", return_value=_service(dead)):
        out = drive_tools.read_drive_file("f-gone")
    assert "itai.samsung.isr@gmail.com" in out
    assert "calendar-bot" not in out


def test_a_real_error_does_not_fall_through_to_the_bot(monkeypatch):
    """The second identity is for clean misses (403/404). A 500 from Google is
    a real failure - retrying it as the bot would only double the noise."""
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", SA_JSON)
    broken = MagicMock()
    broken.get.return_value.execute.side_effect = _missing(500)
    with patch.object(drive_tools, "_drive_service", return_value=_service(broken)), \
         patch.object(drive_tools, "_sa_drive_service") as sa:
        out = drive_tools.read_drive_file("f1")
    sa.assert_not_called()
    assert "נכשלה" in out


def test_the_service_account_identity_is_read_from_the_key(monkeypatch):
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", SA_JSON)
    assert drive_tools._sa_email() == "calendar-bot@proj.iam.gserviceaccount.com"
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    assert drive_tools._sa_email() == ""


def test_the_service_account_path_is_read_only_by_scope_and_by_use():
    """drive.readonly is the only scope the SA credentials ever ask for, and the
    SA service is never handed to create, update, delete or permissions - the
    bot's identity can read a shared file and nothing else."""
    assert drive_tools.SA_SCOPES == ["https://www.googleapis.com/auth/drive.readonly"]
    tree = _tree()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in ("create", "update", "delete"):
            source = ast.dump(node)
            assert "_sa_drive_service" not in source


def test_the_bot_path_does_not_touch_the_calendar_module():
    """The two modules share an env var and nothing else: calendar_tools keeps
    its own credentials with its own scope, and drive_tools must not import it."""
    imported = {
        node.names[0].name
        for node in ast.walk(_tree())
        if isinstance(node, ast.Import)
    } | {
        node.module
        for node in ast.walk(_tree())
        if isinstance(node, ast.ImportFrom)
    }
    assert "calendar_tools" not in imported


# --- filing shared files into the working folder ------------------------------

def test_a_shared_file_is_filed_as_a_shortcut_in_the_working_folder():
    """Shortcuts point at the original - no copy, no quota, the owner's updates
    keep showing through. And like every create in this module, it must land in
    the working folder or not happen."""
    files = _files(
        get={"name": "מלאי סניפים"},
        create={"id": "sc1", "name": "מלאי סניפים"},
    )
    with patch.object(drive_tools, "FOLDER_ID", FOLDER), \
         patch.object(drive_tools, "_drive_service", return_value=_service(files)):
        out = drive_tools.save_to_drive_folder("f-shared")
    kwargs = files.create.call_args.kwargs
    assert kwargs["body"]["mimeType"] == "application/vnd.google-apps.shortcut"
    assert kwargs["body"]["shortcutDetails"] == {"targetId": "f-shared"}
    assert kwargs["body"]["parents"] == [FOLDER]
    assert "מלאי סניפים" in out


def test_filing_falls_back_to_the_bot_for_bot_only_shares(monkeypatch):
    """A file shared only with the bot's address is invisible to his token, but
    the bot can still file it - if the working folder was shared with the bot
    as Editor."""
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", SA_JSON)
    dead = MagicMock()
    dead.get.return_value.execute.side_effect = _missing()
    bot = _files(get={"name": "דוח שבועי"}, create={"id": "sc2", "name": "דוח שבועי"})
    with patch.object(drive_tools, "FOLDER_ID", FOLDER), \
         patch.object(drive_tools, "_drive_service", return_value=_service(dead)), \
         patch.object(drive_tools, "_sa_drive_service", return_value=_service(bot)):
        out = drive_tools.save_to_drive_folder("f-bot-only")
    assert "נשמר" in out and "הבוט" in out


def test_filing_a_file_neither_identity_sees_says_to_share_first(monkeypatch):
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", SA_JSON)
    dead = MagicMock()
    dead.get.return_value.execute.side_effect = _missing()
    with patch.object(drive_tools, "FOLDER_ID", FOLDER), \
         patch.object(drive_tools, "_drive_service", return_value=_service(dead)), \
         patch.object(drive_tools, "_sa_drive_service", return_value=_service(dead)):
        out = drive_tools.save_to_drive_folder("f-gone")
    assert "שתף אותו קודם" in out
    dead.create.assert_not_called()


def test_filing_without_a_working_folder_is_refused():
    with patch.object(drive_tools, "FOLDER_ID", ""):
        out = drive_tools.save_to_drive_folder("f1")
    assert "לא הוגדרה תיקיית עבודה" in out


def test_the_filing_tool_is_registered_and_the_prompt_knows_the_rule():
    import assistant
    assert assistant.save_to_drive_folder in assistant.tools_list
    assert "save_to_drive_folder" in assistant.SYSTEM_PROMPT
    assert "one folder, nowhere else" in assistant.SYSTEM_PROMPT


# --- files shared with the bot itself -----------------------------------------

def test_listing_bot_shares_asks_the_bot_identity_not_his_drive(monkeypatch):
    """'What did I share with you?' is the bot's sharedWithMe set, asked of the
    service account - never a listing of his Drive, which is how the bot once
    answered with his whole library."""
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", SA_JSON)
    files = _files(list={"files": [
        {"id": "s1", "name": "מכירות שבוע 36", "mimeType": "application/vnd.google-apps.spreadsheet",
         "modifiedTime": "2026-09-07T10:00:00Z", "owners": [{"displayName": "איתי"}]},
    ]})
    user_service = MagicMock()
    with patch.object(drive_tools, "_sa_drive_service", return_value=_service(files)), \
         patch.object(drive_tools, "_drive_service", return_value=user_service):
        out = drive_tools.list_bot_shares()
    assert files.list.call_args.kwargs["q"] == "sharedWithMe = true and trashed = false"
    assert "מכירות שבוע 36" in out
    user_service.files.assert_not_called()


def test_listing_bot_shares_without_a_bot_identity_says_so(monkeypatch):
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    out = drive_tools.list_bot_shares()
    assert "GOOGLE_SERVICE_ACCOUNT_JSON" in out


def test_an_empty_share_list_says_nothing_was_shared_and_with_whom(monkeypatch):
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", SA_JSON)
    files = _files(list={"files": []})
    with patch.object(drive_tools, "_sa_drive_service", return_value=_service(files)):
        out = drive_tools.list_bot_shares()
    assert "עוד לא שותף" in out and drive_tools._sa_email() in out


def test_the_bot_shares_tool_is_registered_and_the_prompt_knows_the_rule():
    import assistant
    assert assistant.list_bot_shares in assistant.tools_list
    assert "list_bot_shares" in assistant.SYSTEM_PROMPT


# --- mirroring bot shares into the working folder ---------------------------

def test_mirror_adds_the_working_folder_as_a_parent_to_each_shared_file(monkeypatch):
    """Option two, as Itai chose it: the same file in two places, never a copy -
    so his edits keep showing through and revoking the share still cuts the bot
    off. A file already there, a shared folder, and the working folder itself
    are all left alone."""
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", SA_JSON)
    write = _files(update={"id": "f1"})
    items = [
        {"id": "f1", "name": "דוח שבועי", "mimeType": "application/vnd.google-apps.document",
         "parents": ["his-root"]},
        {"id": "f2", "name": "מצגת", "mimeType": "application/vnd.google-apps.presentation",
         "parents": [FOLDER]},
        {"id": "fold", "name": "תיקייה משותפת", "mimeType": drive_tools.FOLDER_MIME,
         "parents": ["his-root"]},
        {"id": FOLDER, "name": "תיקיית הבוט", "mimeType": drive_tools.FOLDER_MIME, "parents": []},
    ]
    with patch.object(drive_tools, "FOLDER_ID", FOLDER), \
         patch.object(drive_tools, "_sa_write_drive_service", return_value=_service(write)):
        stats = drive_tools.mirror_bot_shares(items)
    write.update.assert_called_once()
    kwargs = write.update.call_args.kwargs
    assert kwargs["fileId"] == "f1"
    assert kwargs["addParents"] == FOLDER
    assert kwargs["supportsAllDrives"] is True
    assert stats["added"] == ["דוח שבועי"]
    assert stats["already"] == 1
    assert stats["failed"] == []


def test_a_share_that_cannot_be_reparented_is_counted_not_raised(monkeypatch):
    """A file shared as view-only cannot gain a parent - Google answers 403.
    The mirror is bookkeeping: one refusal must not stop the rest, and must
    never take down the listing that called it."""
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", SA_JSON)
    write = MagicMock()
    write.update.return_value.execute.side_effect = _missing(403)
    items = [
        {"id": "v1", "name": "לצפייה בלבד", "mimeType": "text/plain", "parents": []},
        {"id": "v2", "name": "גם זה", "mimeType": "text/plain", "parents": []},
    ]
    with patch.object(drive_tools, "FOLDER_ID", FOLDER), \
         patch.object(drive_tools, "_sa_write_drive_service", return_value=_service(write)):
        stats = drive_tools.mirror_bot_shares(items)
    assert write.update.call_count == 2
    assert stats["failed"] == ["לצפייה בלבד", "גם זה"]
    assert stats["added"] == []


def test_mirror_without_a_working_folder_or_a_bot_identity_does_nothing(monkeypatch):
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", SA_JSON)
    with patch.object(drive_tools, "FOLDER_ID", ""):
        stats = drive_tools.mirror_bot_shares([{"id": "f1", "mimeType": "text/plain"}])
    assert stats == {"added": [], "already": 0, "failed": []}


def test_the_write_scope_is_used_only_by_the_mirror():
    """The bot's identity may write in exactly one way: adding the working
    folder as a parent of a file already shared with it. The wider scope
    exists for that call alone, so any other function reaching for the write
    service fails this test."""
    assert drive_tools.SA_WRITE_SCOPES == ["https://www.googleapis.com/auth/drive"]
    users = set()
    for node in ast.walk(_tree()):
        if isinstance(node, ast.FunctionDef):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name) \
                        and inner.func.id == "_sa_write_drive_service":
                    users.add(node.name)
    assert users == {"mirror_bot_shares"}, f"the write identity leaked into {users}"


def test_the_mirror_write_only_reparents_never_edits():
    """addParents changes where a file shows up, not what is inside it. A
    mirror update carrying a body or media would be an edit wearing the
    mirror's clothes."""
    found = False
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "update" \
                and any(kw.arg == "addParents" for kw in node.keywords):
            found = True
            keywords = {kw.arg for kw in node.keywords}
            assert "body" not in keywords and "media_body" not in keywords
    assert found, "the mirror's addParents update is gone"


# --- the reworked listing ----------------------------------------------------

def test_listing_bot_shares_walks_every_page_so_old_shares_are_not_cut_off(monkeypatch):
    """The listing used to stop after one page of 15, ordered by last edit -
    a share of a file nobody had edited lately fell off the end and the bot
    reported it as never shared. It walks every page now, and says WHEN each
    share happened so 'only new ones show' cannot be claimed again."""
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", SA_JSON)
    files = MagicMock()
    files.list.return_value.execute.side_effect = [
        {"files": [
            {"id": "n1", "name": "קובץ חדש", "mimeType": "text/plain",
             "modifiedTime": "2026-09-08T10:00:00Z", "sharedWithMeTime": "2026-09-08T10:05:00Z",
             "parents": [FOLDER]},
        ], "nextPageToken": "p2"},
        {"files": [
            {"id": "o1", "name": "מסמך ישן", "mimeType": "text/plain",
             "modifiedTime": "2026-01-01T10:00:00Z", "sharedWithMeTime": "2026-09-01T09:00:00Z",
             "parents": [FOLDER]},
        ]},
    ]
    with patch.object(drive_tools, "FOLDER_ID", FOLDER), \
         patch.object(drive_tools, "_sa_drive_service", return_value=_service(files)), \
         patch.object(drive_tools, "_sa_write_drive_service", return_value=_service(MagicMock())):
        out = drive_tools.list_bot_shares()
    assert files.list.call_count == 2
    assert files.list.call_args_list[1].kwargs["pageToken"] == "p2"
    assert "מסמך ישן" in out
    assert "שותף ב: 2026-09-01" in out


def test_a_shared_folder_is_listed_with_its_contents(monkeypatch):
    """Files inside a shared folder are not in the bot's sharedWithMe set -
    the folder share extends to them without naming them. 'What did I share
    with you' has to open the folder, or a folder-share looks invisible."""
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", SA_JSON)
    files = MagicMock()
    files.list.return_value.execute.side_effect = [
        {"files": [
            {"id": "shared-fold", "name": "חומרי סניף", "mimeType": drive_tools.FOLDER_MIME,
             "modifiedTime": "2026-09-07T10:00:00Z", "sharedWithMeTime": "2026-09-07T10:00:00Z",
             "parents": []},
        ]},
        {"files": [
            {"id": "in1", "name": "מחירון", "mimeType": "text/plain",
             "modifiedTime": "2026-09-07T11:00:00Z"},
        ]},
    ]
    with patch.object(drive_tools, "FOLDER_ID", FOLDER), \
         patch.object(drive_tools, "_sa_drive_service", return_value=_service(files)), \
         patch.object(drive_tools, "_sa_write_drive_service", return_value=_service(MagicMock())):
        out = drive_tools.list_bot_shares()
    assert "חומרי סניף" in out and "מחירון" in out
    assert "'shared-fold' in parents" in files.list.call_args_list[1].kwargs["q"]


def test_the_working_folder_itself_is_neither_expanded_nor_mirrored(monkeypatch):
    """He shares the working folder with the bot so the bot can write there.
    Listing its contents as 'shared with the bot' would be noise, and adding
    it as its own parent is a cycle Drive rejects."""
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", SA_JSON)
    files = _files(list={"files": [
        {"id": FOLDER, "name": "תיקיית הבוט", "mimeType": drive_tools.FOLDER_MIME,
         "modifiedTime": "2026-09-07T10:00:00Z", "parents": []},
    ]})
    write = _files(update={"id": "x"})
    with patch.object(drive_tools, "FOLDER_ID", FOLDER), \
         patch.object(drive_tools, "_sa_drive_service", return_value=_service(files)), \
         patch.object(drive_tools, "_sa_write_drive_service", return_value=_service(write)):
        out = drive_tools.list_bot_shares()
    assert files.list.call_count == 1, "the working folder's contents were listed as shares"
    write.update.assert_not_called()
    assert "תיקיית העבודה של הבוט" in out


def test_a_clean_listing_says_the_files_also_show_in_the_working_folder(monkeypatch):
    """The tracking he asked for: the answer itself points at the one place
    in his Drive that always reflects what the bot can see."""
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", SA_JSON)
    files = _files(list={"files": [
        {"id": "s1", "name": "מכירות שבוע 36", "mimeType": "application/vnd.google-apps.spreadsheet",
         "modifiedTime": "2026-09-07T10:00:00Z", "parents": [FOLDER]},
    ]})
    with patch.object(drive_tools, "FOLDER_ID", FOLDER), \
         patch.object(drive_tools, "_sa_drive_service", return_value=_service(files)), \
         patch.object(drive_tools, "_sa_write_drive_service", return_value=_service(MagicMock())):
        out = drive_tools.list_bot_shares()
    assert "מכירות שבוע 36" in out
    assert "תיקיית העבודה" in out


def test_a_failed_mirror_is_reported_without_hiding_the_listing(monkeypatch):
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", SA_JSON)
    files = _files(list={"files": [
        {"id": "s1", "name": "מסמך לצפייה", "mimeType": "text/plain",
         "modifiedTime": "2026-09-07T10:00:00Z", "parents": []},
    ]})
    write = MagicMock()
    write.update.return_value.execute.side_effect = _missing(403)
    with patch.object(drive_tools, "FOLDER_ID", FOLDER), \
         patch.object(drive_tools, "_sa_drive_service", return_value=_service(files)), \
         patch.object(drive_tools, "_sa_write_drive_service", return_value=_service(write)):
        out = drive_tools.list_bot_shares()
    assert "מסמך לצפייה" in out
    assert "צפייה בלבד" in out


def test_the_prompt_knows_shares_are_mirrored_and_dated():
    """The model cannot relay what it was never told: the answer rules must
    say that shares are mirrored into the working folder and that the listing
    covers old shares too, or 'nothing new today' comes back."""
    import assistant
    assert "mirrored into the working folder" in assistant.SYSTEM_PROMPT
    assert "every share, old and new" in assistant.SYSTEM_PROMPT
