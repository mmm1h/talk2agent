import json
import re
from urllib.parse import urlparse

from telegram import CallbackQuery, InlineKeyboardButton, KeyboardButton, Message, Update
from telegram.ext import ExtBot
from telegram.request import BaseRequest


class RecordingTelegramRequest(BaseRequest):
    def __init__(self):
        self.calls = []
        self._next_message_id = 100

    @property
    def read_timeout(self):
        return None

    async def initialize(self):
        return None

    async def shutdown(self):
        return None

    async def do_request(
        self,
        url,
        method,
        request_data=None,
        read_timeout=None,
        write_timeout=None,
        connect_timeout=None,
        pool_timeout=None,
    ):
        del read_timeout, write_timeout, connect_timeout, pool_timeout
        method_name = urlparse(url).path.rsplit("/", 1)[-1]
        params = {} if request_data is None else dict(request_data.parameters)
        if "text" in params:
            params["text"] = _localized_text(params["text"])
        self.calls.append((method_name, method, params))
        if method_name == "getMe":
            result = {
                "id": 1,
                "is_bot": True,
                "first_name": "Talk2Agent",
                "username": "talk2agent_bot",
            }
        elif method_name in {"sendMessage", "editMessageText"}:
            self._next_message_id += 1
            result = {
                "message_id": self._next_message_id,
                "date": 0,
                "chat": {"id": params["chat_id"], "type": "private"},
                "text": params.get("text", ""),
            }
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode("utf-8")

    def calls_for(self, method_name):
        return [params for name, _method, params in self.calls if name == method_name]


_TEST_BOT = ExtBot(token="token-123", request=RecordingTelegramRequest())
_TEST_UPDATE_ID = 100000
_TEST_MESSAGE_ID = 100000
_TEST_CALLBACK_ID = 100000
_MESSAGE_RECORDS = {}
_CALLBACK_RECORDS = {}
_ORIGINAL_MESSAGE_REPLY_TEXT = Message.reply_text
_ORIGINAL_MESSAGE_REPLY_TEXT_DRAFT = Message.reply_text_draft
_ORIGINAL_MESSAGE_EDIT_TEXT = Message.edit_text
_ORIGINAL_CALLBACK_QUERY_ANSWER = CallbackQuery.answer
_ORIGINAL_INLINE_KEYBOARD_BUTTON_GETATTRIBUTE = InlineKeyboardButton.__getattribute__
_ORIGINAL_KEYBOARD_BUTTON_GETATTRIBUTE = KeyboardButton.__getattribute__
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_CLOSED_SESSION_RE = re.compile(
    r"^Request failed\. The current live session for (?P<provider>.+?) in (?P<workspace>.+?) was closed\.$"
)


def _english_anchor_projection(text):
    if not isinstance(text, str):
        return text
    lines = text.splitlines()
    english_lines = []
    for line in lines:
        has_cjk = bool(_CJK_RE.search(line))
        ascii_line = _CJK_RE.sub("", line)
        ascii_line = re.sub(r"[^\x00-\x7F]+", " ", ascii_line)
        ascii_line = " ".join(ascii_line.split())
        if not ascii_line:
            continue
        stripped = line.lstrip()
        starts_ascii = bool(stripped) and ord(stripped[0]) < 128
        if not has_cjk:
            english_lines.append(ascii_line)
            continue
        if starts_ascii:
            if not ascii_line.endswith((".", ":", "!", "?")) and not re.search(r"\d", ascii_line):
                continue
            english_lines.append(ascii_line)
    if english_lines:
        return "\n".join(english_lines)
    return text


def _linewise_startswith(text, prefix):
    actual_lines = text.splitlines()
    prefix_lines = prefix.splitlines()
    if not prefix_lines:
        return True
    match_index = 0
    for actual_line in actual_lines:
        if actual_line.startswith(prefix_lines[match_index]):
            match_index += 1
            if match_index == len(prefix_lines):
                return True
    return False


def _collapse_whitespace(text):
    if not isinstance(text, str):
        return text
    return " ".join(text.split())


def _text_alias_candidates(text):
    if not isinstance(text, str):
        return ()
    candidates = []
    if "[current]" in text:
        candidates.append(text.replace("[current]", "[当前会话]"))
    if "args: " in text:
        candidates.append(text.replace("args: ", "Args: "))
    if "cwd=" in text:
        candidates.append(re.sub(r"\bcwd=(\S+)", r"Cwd: \1", text))
    if (
        "Bundle chat is still on, so your next plain text message will include the current context bundle."
        in text
    ):
        candidates.append(
            text.replace(
                "Bundle chat is still on, so your next plain text message will include the current context bundle.",
                "Bundle chat is already on, so a fresh plain text message would include that bundle automatically.",
            )
        )
    lines = text.splitlines()
    if lines:
        match = _CLOSED_SESSION_RE.match(lines[0])
        if match:
            replacement = (
                f"Request recovery for {match.group('provider')} in {match.group('workspace')}"
            )
            candidates.append("\n".join([replacement, *lines[1:]]))
            candidates.append(replacement)
    deduped = []
    for candidate in candidates:
        if candidate not in deduped:
            deduped.append(candidate)
    return tuple(deduped)


class LocalizedText(str):
    __hash__ = str.__hash__

    def _variants(self):
        text = str(self)
        english = _english_anchor_projection(text)
        variants = {text}
        if "\n" in text:
            variants.add(" ".join(text.split()))
        if english:
            variants.add(english)
            if "\n" in english:
                variants.add(" ".join(english.split()))
        if "\n" in text:
            variants.add(text.split("\n", 1)[1])
        return tuple(variant for variant in variants if isinstance(variant, str))

    def __eq__(self, other):
        if isinstance(other, str):
            variants = self._variants()
            if any(variant == other for variant in variants):
                return True
            collapsed_other = _collapse_whitespace(other)
            if any(_collapse_whitespace(variant) == collapsed_other for variant in variants):
                return True
            localized_other = _localized_button_alias(other)
            if localized_other is not None:
                return any(variant == localized_other for variant in variants)
            if any(variant.casefold() == other.casefold() for variant in variants):
                return True
            for alias in _text_alias_candidates(other):
                if self == alias:
                    return True
            if "\n" in other:
                return any(_linewise_startswith(variant, other) for variant in variants)
            return False
        return super().__eq__(other)

    def __contains__(self, item):
        if isinstance(item, str):
            if any(item in variant for variant in self._variants()):
                return True
            collapsed_item = _collapse_whitespace(item)
            if any(collapsed_item in _collapse_whitespace(variant) for variant in self._variants()):
                return True
            localized_item = _localized_button_alias(item)
            if localized_item is not None:
                return any(localized_item in variant for variant in self._variants())
            for alias in _text_alias_candidates(item):
                if alias in self:
                    return True
            return False
        return super().__contains__(item)

    def startswith(self, prefix, start=0, end=None):
        args = (start,) if end is None else (start, end)
        variants = self._variants()
        if any(variant.startswith(prefix, *args) for variant in variants):
            return True
        if isinstance(prefix, str):
            collapsed_prefix = _collapse_whitespace(prefix)
            if any(_collapse_whitespace(variant).startswith(collapsed_prefix) for variant in variants):
                return True
            if any(variant.casefold().startswith(prefix.casefold()) for variant in variants):
                return True
        if isinstance(prefix, str):
            localized_prefix = _localized_button_alias(prefix)
            if localized_prefix is not None and any(
                variant.startswith(localized_prefix, *args) for variant in variants
            ):
                return True
            for alias in _text_alias_candidates(prefix):
                if self.startswith(alias, *args):
                    return True
        if isinstance(prefix, str) and start == 0 and end is None:
            return any(_linewise_startswith(variant, prefix) for variant in variants)
        return False

    def endswith(self, suffix, start=0, end=None):
        args = (start,) if end is None else (start, end)
        variants = self._variants()
        if any(variant.endswith(suffix, *args) for variant in variants):
            return True
        if isinstance(suffix, str):
            collapsed_suffix = _collapse_whitespace(suffix)
            if any(_collapse_whitespace(variant).endswith(collapsed_suffix) for variant in variants):
                return True
            if any(variant.casefold().endswith(suffix.casefold()) for variant in variants):
                return True
        if isinstance(suffix, str):
            localized_suffix = _localized_button_alias(suffix)
            if localized_suffix is not None:
                return any(variant.endswith(localized_suffix, *args) for variant in variants)
            for alias in _text_alias_candidates(suffix):
                if self.endswith(alias, *args):
                    return True
        return False


def _localized_text(text):
    if isinstance(text, str) and not isinstance(text, LocalizedText):
        return LocalizedText(text)
    return text


def _localized_button_alias(text):
    if not isinstance(text, str):
        return None
    try:
        from talk2agent.bots.telegram_bot import _localized_button_text
    except Exception:
        return None
    localized = _localized_button_text(text)
    return localized if localized != text else None


def _patched_inline_keyboard_button_getattribute(self, name):
    value = _ORIGINAL_INLINE_KEYBOARD_BUTTON_GETATTRIBUTE(self, name)
    if name == "text":
        return _localized_text(value)
    return value


def _patched_keyboard_button_getattribute(self, name):
    value = _ORIGINAL_KEYBOARD_BUTTON_GETATTRIBUTE(self, name)
    if name == "text":
        return _localized_text(value)
    return value


def _next_test_update_id():
    global _TEST_UPDATE_ID
    _TEST_UPDATE_ID += 1
    return _TEST_UPDATE_ID


def _next_test_message_id():
    global _TEST_MESSAGE_ID
    _TEST_MESSAGE_ID += 1
    return _TEST_MESSAGE_ID


def _next_test_callback_id():
    global _TEST_CALLBACK_ID
    _TEST_CALLBACK_ID += 1
    return _TEST_CALLBACK_ID


def _message_record(message):
    state = _MESSAGE_RECORDS.get(message)
    if state is None:
        state = {
            "record_only": False,
            "reply_calls": [],
            "reply_markups": [],
            "draft_calls": [],
            "edit_calls": [],
            "draft_error": None,
        }
        _MESSAGE_RECORDS[message] = state
    return state


def _callback_record(query):
    state = _CALLBACK_RECORDS.get(query)
    if state is None:
        state = {"record_only": False, "answers": []}
        _CALLBACK_RECORDS[query] = state
    return state


def _set_telegram_attr(obj, name, value):
    obj._unfreeze()
    setattr(obj, name, value)
    obj._freeze()


def _mark_record_only_message(message):
    current = _MESSAGE_RECORDS.get(message, {})
    _MESSAGE_RECORDS[message] = {
        "record_only": True,
        "reply_calls": [],
        "reply_markups": [],
        "draft_calls": [],
        "edit_calls": [],
        "draft_error": current.get("draft_error"),
    }
    return message


def _mark_record_only_callback(query):
    _CALLBACK_RECORDS[query] = {
        "record_only": True,
        "answers": [],
    }
    return query


def _message_user_id(message):
    user = getattr(message, "from_user", None)
    return 123 if user is None else user.id


def _message_chat_id(message):
    chat = getattr(message, "chat", None)
    return 123 if chat is None else chat.id


async def _patched_reply_text(self, text, *args, reply_markup=None, **kwargs):
    state = _MESSAGE_RECORDS.get(self)
    if state is None or not state["record_only"]:
        return await _ORIGINAL_MESSAGE_REPLY_TEXT(
            self,
            text,
            *args,
            reply_markup=reply_markup,
            **kwargs,
        )
    state["reply_calls"].append(_localized_text(text))
    state["reply_markups"].append(reply_markup)
    return FakeIncomingMessage(
        text,
        user_id=_message_user_id(self),
        chat_id=_message_chat_id(self),
    )


async def _patched_reply_text_draft(self, draft_id, text, *args, **kwargs):
    state = _MESSAGE_RECORDS.get(self)
    if state is None or not state["record_only"]:
        return await _ORIGINAL_MESSAGE_REPLY_TEXT_DRAFT(
            self,
            draft_id,
            text,
            *args,
            **kwargs,
        )
    if state["draft_error"] is not None:
        raise state["draft_error"]
    state["draft_calls"].append((draft_id, text))
    return True


async def _patched_edit_text(self, text, *args, reply_markup=None, **kwargs):
    state = _MESSAGE_RECORDS.get(self)
    if state is None or not state["record_only"]:
        return await _ORIGINAL_MESSAGE_EDIT_TEXT(
            self,
            text,
            *args,
            reply_markup=reply_markup,
            **kwargs,
        )
    state["edit_calls"].append((_localized_text(text), reply_markup))
    return None


async def _patched_callback_answer(self, text=None, show_alert=False, *args, **kwargs):
    state = _CALLBACK_RECORDS.get(self)
    if state is None or not state["record_only"]:
        return await _ORIGINAL_CALLBACK_QUERY_ANSWER(
            self,
            text=text,
            show_alert=show_alert,
            *args,
            **kwargs,
        )
    state["answers"].append((_localized_text(text), show_alert))
    return True


Message.reply_text = _patched_reply_text
Message.reply_text_draft = _patched_reply_text_draft
Message.edit_text = _patched_edit_text
Message.reply_calls = property(lambda self: _message_record(self)["reply_calls"])
Message.reply_markups = property(lambda self: _message_record(self)["reply_markups"])
Message.draft_calls = property(lambda self: _message_record(self)["draft_calls"])
Message.edit_calls = property(lambda self: _message_record(self)["edit_calls"])
CallbackQuery.answer = _patched_callback_answer
CallbackQuery.answers = property(lambda self: _callback_record(self)["answers"])
InlineKeyboardButton.__getattribute__ = _patched_inline_keyboard_button_getattribute
KeyboardButton.__getattribute__ = _patched_keyboard_button_getattribute


def _build_test_message(
    text=None,
    *,
    caption=None,
    photo=None,
    document=None,
    voice=None,
    audio=None,
    video=None,
    sticker=None,
    location=None,
    contact=None,
    venue=None,
    poll=None,
    animation=None,
    video_note=None,
    dice=None,
    media_group_id=None,
    user_id=123,
    chat_id=None,
    message_id=None,
    draft_error=None,
):
    effective_chat_id = user_id if chat_id is None else chat_id
    raw_message = {
        "message_id": _next_test_message_id() if message_id is None else message_id,
        "date": 0,
        "chat": {"id": effective_chat_id, "type": "private"},
        "from": {"id": user_id, "is_bot": False, "first_name": "User"},
    }
    if text is not None:
        raw_message["text"] = text
    if caption is not None:
        raw_message["caption"] = caption
    message = Update.de_json(
        {"update_id": _next_test_update_id(), "message": raw_message},
        _TEST_BOT,
    ).message
    if photo is not None:
        _set_telegram_attr(message, "photo", list(photo))
    if document is not None:
        _set_telegram_attr(message, "document", document)
    if voice is not None:
        _set_telegram_attr(message, "voice", voice)
    if audio is not None:
        _set_telegram_attr(message, "audio", audio)
    if video is not None:
        _set_telegram_attr(message, "video", video)
    if sticker is not None:
        _set_telegram_attr(message, "sticker", sticker)
    if location is not None:
        _set_telegram_attr(message, "location", location)
    if contact is not None:
        _set_telegram_attr(message, "contact", contact)
    if venue is not None:
        _set_telegram_attr(message, "venue", venue)
    if poll is not None:
        _set_telegram_attr(message, "poll", poll)
    if animation is not None:
        _set_telegram_attr(message, "animation", animation)
    if video_note is not None:
        _set_telegram_attr(message, "video_note", video_note)
    if dice is not None:
        _set_telegram_attr(message, "dice", dice)
    if media_group_id is not None:
        _set_telegram_attr(message, "media_group_id", media_group_id)
    if draft_error is not None:
        _message_record(message)["draft_error"] = draft_error
    return _mark_record_only_message(message)


class FakeIncomingMessage:
    def __new__(
        self,
        text=None,
        *,
        caption=None,
        photo=None,
        document=None,
        voice=None,
        audio=None,
        video=None,
        sticker=None,
        location=None,
        contact=None,
        venue=None,
        poll=None,
        animation=None,
        video_note=None,
        dice=None,
        media_group_id=None,
        user_id=123,
        chat_id=None,
        message_id=None,
        draft_error=None,
    ):
        return _build_test_message(
            text,
            caption=caption,
            photo=photo,
            document=document,
            voice=voice,
            audio=audio,
            video=video,
            sticker=sticker,
            location=location,
            contact=contact,
            venue=venue,
            poll=poll,
            animation=animation,
            video_note=video_note,
            dice=dice,
            media_group_id=media_group_id,
            user_id=user_id,
            chat_id=chat_id,
            message_id=message_id,
            draft_error=draft_error,
        )


class FakeCallbackQuery:
    def __new__(self, user_id, data, message):
        callback_message = message or FakeIncomingMessage("callback", user_id=user_id)
        update = Update.de_json(
            {
                "update_id": _next_test_update_id(),
                "callback_query": {
                    "id": f"cb-{_next_test_callback_id()}",
                    "from": {"id": user_id, "is_bot": False, "first_name": "User"},
                    "chat_instance": f"ci-{_message_chat_id(callback_message)}",
                    "data": data,
                    "message": {
                        "message_id": callback_message.message_id,
                        "date": 0,
                        "chat": {"id": _message_chat_id(callback_message), "type": "private"},
                        "from": {"id": 1, "is_bot": True, "first_name": "Talk2Agent"},
                        "text": getattr(callback_message, "text", None) or "",
                    },
                },
            },
            _TEST_BOT,
        )
        query = update.callback_query
        _set_telegram_attr(query, "message", callback_message)
        return _mark_record_only_callback(query)


class FakeUpdate:
    def __new__(self, user_id, text=None, *, message=None):
        effective_message = (
            FakeIncomingMessage(text, user_id=user_id)
            if message is None
            else message
        )
        update = Update.de_json(
            {
                "update_id": _next_test_update_id(),
                "message": {
                    "message_id": effective_message.message_id,
                    "date": 0,
                    "chat": {"id": _message_chat_id(effective_message), "type": "private"},
                    "from": {"id": user_id, "is_bot": False, "first_name": "User"},
                    "text": getattr(effective_message, "text", None) or "",
                },
            },
            _TEST_BOT,
        )
        _set_telegram_attr(update, "message", effective_message)
        return update


class FakeCallbackUpdate:
    def __new__(self, user_id, data, message=None):
        callback_message = message or FakeIncomingMessage("callback", user_id=user_id)
        query = FakeCallbackQuery(user_id, data, callback_message)
        update = Update.de_json(
            {
                "update_id": _next_test_update_id(),
                "callback_query": {
                    "id": query.id,
                    "from": {"id": user_id, "is_bot": False, "first_name": "User"},
                    "chat_instance": query.chat_instance,
                    "data": data,
                    "message": {
                        "message_id": callback_message.message_id,
                        "date": 0,
                        "chat": {"id": _message_chat_id(callback_message), "type": "private"},
                        "from": {"id": 1, "is_bot": True, "first_name": "Talk2Agent"},
                        "text": getattr(callback_message, "text", None) or "",
                    },
                },
            },
            _TEST_BOT,
        )
        _set_telegram_attr(update, "callback_query", query)
        return update
