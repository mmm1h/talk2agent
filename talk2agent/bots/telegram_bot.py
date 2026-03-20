from __future__ import annotations

import time
from functools import partial

from telegram import Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

from talk2agent.bots.telegram_stream import TelegramTurnStream
from talk2agent.session_store import RetiredSessionStoreError


def _is_authorized(update: Update, services) -> bool:
    user = update.effective_user
    return user is not None and user.id in services.allowed_user_ids


async def _reply_unauthorized(update: Update) -> None:
    if update.message is not None:
        await update.message.reply_text("Unauthorized user.")


async def _reply_request_failed(update: Update) -> None:
    if update.message is not None:
        await update.message.reply_text("Request failed.")


async def _with_active_store(services, action):
    last_error = None
    for _ in range(2):
        state = await services.snapshot_runtime_state()
        try:
            result = await action(state.session_store)
        except RetiredSessionStoreError as exc:
            last_error = exc
            continue
        return state, result
    raise last_error or RuntimeError("retired store retry exhausted")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE, services) -> None:
    del context

    if update.message is None:
        return
    if not _is_authorized(update, services):
        await _reply_unauthorized(update)
        return

    try:
        state, session = await _with_active_store(
            services,
            lambda store: _prepare_turn_session(
                store,
                update.effective_user.id,
                time.monotonic(),
            ),
        )
    except Exception:
        await _reply_request_failed(update)
        return

    placeholder = await update.message.reply_text("Thinking...")
    stream = TelegramTurnStream(
        placeholder=placeholder,
        edit_interval=services.config.runtime.stream_edit_interval_ms / 1000.0,
    )
    try:
        response = await session.run_turn(update.message.text, stream)
    except Exception:
        invalidate = getattr(state.session_store, "invalidate", None)
        try:
            if invalidate is not None:
                await invalidate(update.effective_user.id, session)
            else:
                await session.close()
        except Exception:
            pass
        await stream.fail("Request failed.")
        return

    await stream.finish(stop_reason=response.stop_reason)


async def handle_new(update: Update, context: ContextTypes.DEFAULT_TYPE, services) -> None:
    del context

    if update.message is None:
        return
    if not _is_authorized(update, services):
        await _reply_unauthorized(update)
        return

    try:
        _, session = await _with_active_store(
            services,
            lambda store: store.reset(update.effective_user.id),
        )
    except Exception:
        await _reply_request_failed(update)
        return

    await update.message.reply_text(f"Started new session: {session.session_id or 'pending'}")


async def handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE, services) -> None:
    del context

    if update.message is None:
        return
    if not _is_authorized(update, services):
        await _reply_unauthorized(update)
        return

    try:
        state, session = await _with_active_store(
            services,
            lambda store: store.peek(update.effective_user.id),
        )
    except Exception:
        await _reply_request_failed(update)
        return

    if session is None:
        session_id = "none"
    else:
        session_id = session.session_id or "pending"
    await update.message.reply_text(f"provider={state.provider} session_id={session_id}")


async def handle_provider(update: Update, context: ContextTypes.DEFAULT_TYPE, services) -> None:
    if update.message is None:
        return
    if not _is_authorized(update, services):
        await _reply_unauthorized(update)
        return
    if update.effective_user is None or update.effective_user.id != services.admin_user_id:
        await _reply_unauthorized(update)
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /provider <claude|codex|gemini>")
        return

    try:
        provider = await services.switch_provider(context.args[0])
    except ValueError:
        await update.message.reply_text("Usage: /provider <claude|codex|gemini>")
        return
    except Exception:
        await _reply_request_failed(update)
        return

    await update.message.reply_text(f"provider={provider}")


def build_telegram_application(config, services) -> Application:
    application = ApplicationBuilder().token(config.telegram.bot_token).build()
    application.add_handler(CommandHandler("new", partial(handle_new, services=services)))
    application.add_handler(CommandHandler("status", partial(handle_status, services=services)))
    application.add_handler(CommandHandler("provider", partial(handle_provider, services=services)))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, partial(handle_text, services=services))
    )
    return application


async def _prepare_turn_session(store, user_id: int, now: float):
    await store.close_idle_sessions(now)
    return await store.get_or_create(user_id)
