import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.api import team_chat
from app.api.team_chat import (
    ChatSend,
    _address_bot_reply,
    _claims_product_prices,
    _group_message_requests,
    _group_message_tool,
    _implicit_group_target,
    _reply_handoff_targets,
    _unbacked_deferred_work,
    _correct_group_reply_recipient,
    guard_group_reply_evidence,
)
from app.api.workforce import WorkforcePrincipal
from app.core.domain.product_evidence import looks_truncated, product_source_urls, research_result_has_sources
from app.models.agent import Agent
from app.models.session import Session
from app.models.run import Run
from app.models.team_chat import TeamChatMessage


def test_group_reply_does_not_claim_missing_research_data_is_complete():
    guarded = guard_group_reply_evidence(
        "Data sudah masuk, tabel lengkap. Saya akan membuat caption.",
        [],
        "Owner: Tolong tunggu data dari Riset Harga.",
    )

    assert "belum menerima data riset" in guarded
    assert "Saya akan membuat caption" not in guarded


def test_group_reply_keeps_data_claim_when_visible_sources_exist():
    reply = "Data sudah masuk dan tabel lengkap. Saya lanjut menyusun caption."

    assert guard_group_reply_evidence(
        reply,
        [],
        "Riset Harga: | Produk | Harga | Link |\n| A | Rp10.000 | https://example.com/a |\n| B | Rp12.000 | https://example.com/b |",
    ) == reply


def test_group_reply_keeps_data_claim_after_successful_search_tool():
    reply = "Hasil riset sudah diterima."

    assert guard_group_reply_evidence(
        reply,
        [{"tool": "tavily_search", "result": "https://example.com/product/a https://example.com/product/b"}],
        "Owner: Cari harga 2 produk ini.",
    ) == reply


def test_group_reply_rejects_ten_product_claim_with_only_three_links():
    reply = "Data 10 produk udah siap."
    links = "\n".join(f"https://example.com/product-{number}" for number in range(3))

    assert guard_group_reply_evidence(reply, [], links) != reply
    assert guard_group_reply_evidence(reply, [], "Requested: 3 produk\n" + links) != reply
    assert guard_group_reply_evidence("Data lengkap, caption segera dibuat.", [], links + "\nRequested: 10 produk") != "Data lengkap, caption segera dibuat."


def test_unmentioned_research_request_reaches_manager():
    manager, researcher, writer = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    names = {manager: "Arthur", researcher: "Riset Harga", writer: "Penulis Konten"}

    assert _implicit_group_target("Cariin 3 pupuk organik cair, lalu bikin caption", names, manager) == manager
    assert _implicit_group_target("Arthur, koordinasikan tim", names, manager) == manager


def test_any_bot_mention_routes_peer_without_research_evidence():
    manager, researcher, writer = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    names = {manager: "Arthur", researcher: "Riset Harga", writer: "Penulis Konten"}
    reply = "@Penulis Konten tolong susun caption dari data ini."

    assert _reply_handoff_targets(reply, names, researcher) == [writer]
    assert _reply_handoff_targets("@Arthur tolong cek hasil ini.", names, writer) == [manager]
    assert _reply_handoff_targets("@Riset Harga cek dulu, lalu @Penulis Konten rangkum.", names, manager) == [researcher, writer]
    assert _reply_handoff_targets("@Riset Harga ini catatan saya", names, researcher) == []
    assert _reply_handoff_targets("@Penulis Konten dan @Penulis Konten", names, manager) == [writer]
    assert _reply_handoff_targets("@Penulis Kontenan", names, manager) == []


@pytest.mark.asyncio
async def test_specialist_can_message_another_group_member():
    researcher, writer = uuid.uuid4(), uuid.uuid4()
    group_tool = _group_message_tool({"riset harga": researcher, "penulis konten": writer}, researcher)

    result = await group_tool.ainvoke({"agent_names": ["Penulis Konten"], "message": "Buat caption dari tabel ini."})
    requests = _group_message_requests([{"tool": "message_group_members", "result": result}], {researcher, writer}, researcher)

    assert requests == [([writer], "Buat caption dari tabel ini.")]


def test_specialist_reply_addresses_the_actual_sender():
    assert _correct_group_reply_recipient("@Arthur Ini captionnya.", "Arthur", None) == "Ini captionnya."
    assert _correct_group_reply_recipient("@Arthur Tolong cek captionnya.", "Arthur", "Riset Harga") == "@Arthur Tolong cek captionnya."


def test_bot_reply_uses_exact_mention_for_bot_but_not_owner_or_references():
    arthur, researcher, writer = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    names = {arthur: "Arthur", researcher: "Riset Harga", writer: "Copywriter Web"}
    reply = "**Arthur** — Noted, ini inputku.\n\n**Copywriter Web** — sebaiknya kirim draft dulu."

    addressed = _address_bot_reply(reply, arthur, names)
    assert addressed.startswith("@Arthur — Noted")
    assert "\n\n**Copywriter Web** —" in addressed
    assert _address_bot_reply("Untuk pemilik: ini saranku.", arthur, names) == "Untuk pemilik: ini saranku."
    assert _address_bot_reply("Bos, ini laporannya.", arthur, names) == "Bos, ini laporannya."
    assert _address_bot_reply("@Copywriter Web tolong siapkan draft.", arthur, names) == "@Copywriter Web tolong siapkan draft."
    assert _address_bot_reply("Siap, aku kerjakan.", arthur, names).startswith("@Arthur ")


def test_research_sources_require_specific_fresh_product_pages():
    request = "Cari 3 produk pupuk dengan harga minggu ini."
    generic = "INFARM Rp99rb https://www.tokopedia.com/find/pupuk-organik-cair-1-liter"
    assert product_source_urls(generic) == set()
    assert not research_result_has_sources(generic, request)
    result = "\n".join(
        f"Produk {number} Rp{number}0.000 https://www.tokopedia.com/toko/produk-{number}"
        for number in range(1, 4)
    )
    assert research_result_has_sources(result, request)
    assert not research_result_has_sources(result, request, "https://tokopedia.com/toko/produk-1")
    assert research_result_has_sources(result, request, result)
    assert not research_result_has_sources(result + "\nKILAT (dari riset sebelumnya)", request)


def test_obviously_cut_bot_reply_is_not_final():
    assert looks_truncated("Sekarang")
    assert looks_truncated("**Caption IG — pupuk organik")
    assert not looks_truncated("**Caption IG**\nCek tiga pilihan di tabel.")


def test_research_blocker_can_mention_colleague_without_claiming_prices():
    message = "Aku belum bisa mencari produk sekarang. @Arthur tolong cek akses pencarian."
    assert not _claims_product_prices(message)
    assert _claims_product_prices("INFARM Rp99.000 https://tokopedia.com/find/pupuk-organik")


def test_group_future_promise_is_not_mistaken_for_running_work():
    promise = (
        "@Arthur On it. Mulai nulis sekarang. Target draft lengkap "
        "(Beranda, Tentang Kami, Profil Dokter, Kontak & Booking) dalam 1-2 jam. "
        "Begitu jadi, langsung post di sini."
    )
    assert _unbacked_deferred_work(promise, [])
    assert _unbacked_deferred_work(promise, [{"tool": "orchestrate_owner_workforce_task", "result": '{"ok": false}'}])
    assert not _unbacked_deferred_work(promise, [{"tool": "orchestrate_owner_workforce_task", "result": {"ok": True, "task_id": str(uuid.uuid4())}}])
    assert not _unbacked_deferred_work(
        "## Beranda\nSelamat datang di Klinik Gigi Jakarta. " + "Isi nyata. " * 130
        + "\nDraft ini nanti perlu direview pemilik.",
        [],
    )
    assert not _unbacked_deferred_work("Saya belum bisa mulai karena brief belum ada.", [])


@pytest.mark.asyncio
async def test_bot_mentions_trigger_next_bot_and_allow_bounded_reply_to_manager(monkeypatch):
    owner_id, room_id = uuid.uuid4(), uuid.uuid4()
    manager, researcher, writer = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    agents = [SimpleNamespace(id=manager, name="Arthur"),
              SimpleNamespace(id=researcher, name="Riset Harga"),
              SimpleNamespace(id=writer, name="Penulis Konten")]
    sessions = {agent.id: SimpleNamespace(id=uuid.uuid4(), agent_id=agent.id) for agent in agents}
    members = [SimpleNamespace(agent_id=agent.id, session_id=sessions[agent.id].id) for agent in agents]
    room = SimpleNamespace(id=room_id, kind="group", title="QA bot mention", manager_agent_id=manager,
                           workspace_id=str(owner_id), owner_user_id=owner_id, updated_at=datetime.now(timezone.utc))

    class QueryResult:
        def __init__(self, rows):
            self.rows = rows

        def scalar_one_or_none(self):
            return self.rows[0] if self.rows else None

        def scalars(self):
            return self

        def all(self):
            return self.rows

    class FakeDb:
        def __init__(self):
            self.messages = []
            self.current_agent = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, statement):
            entity = statement.column_descriptions[0].get("entity")
            if entity is Agent:
                agent_id = next(value for value in statement.compile().params.values()
                                if isinstance(value, uuid.UUID) and value in sessions)
                self.current_agent = next(agent for agent in agents if agent.id == agent_id)
                return QueryResult([self.current_agent])
            if entity is Session:
                return QueryResult([sessions[self.current_agent.id]])
            if entity is TeamChatMessage:
                return QueryResult([message for message in reversed(self.messages)
                                    if message.sender_type == "agent" and message.content])
            return QueryResult([])

        async def get(self, model, _id):
            return SimpleNamespace(runtime_metadata={}) if model is Run else None

        def add(self, item):
            if isinstance(item, TeamChatMessage):
                item.id = item.id or uuid.uuid4()
                item.created_at = item.created_at or datetime.now(timezone.utc)
                self.messages.append(item)

        async def commit(self):
            pass

        async def rollback(self):
            pass

    db = FakeDb()

    async def get_room(*_args):
        return room

    async def get_members(*_args):
        return members

    async def get_roster(*_args):
        return agents

    async def no_op(*_args, **_kwargs):
        return None

    async def no_dispatch(*_args, **_kwargs):
        return "", []

    @asynccontextmanager
    async def unlocked(*_args):
        yield

    replies = iter([
        "Aku minta Riset Harga cek ide acara ini.",
        "Penulis Konten: buat slogan dari ide acara ini.",
        "@Arthur Target slogan selesai dalam 1-2 jam. Begitu jadi, langsung post di sini.",
        "Arthur — slogan acara sudah jadi: Tumbuh Bersama.",
        "@Riset Harga terima kasih, tugas selesai.",
        "@Arthur slogan sudah saya serahkan.",
    ])
    called = []

    async def fake_run_agent(*, agent_model, user_message, **_kwargs):
        called.append((agent_model.id, user_message))
        steps = ([{"tool": "message_group_members", "result": json.dumps({
            "ok": True, "agent_ids": [str(researcher)], "message": "Cek ide acara ini.",
        })}] if len(called) == 1 else [])
        return {"run_id": uuid.uuid4(), "reply": next(replies), "steps": steps, "tokens_used": 0}

    monkeypatch.setattr(team_chat, "_require_room", get_room)
    monkeypatch.setattr(team_chat, "_room_members", get_members)
    monkeypatch.setattr(team_chat, "_roster", get_roster)
    monkeypatch.setattr(team_chat, "_ensure_member_session", no_op)
    monkeypatch.setattr(team_chat, "_sync_workforce_replies", no_op)
    monkeypatch.setattr(team_chat, "_agent_task_snapshot", no_op)
    monkeypatch.setattr(team_chat, "fill_missing_arthur_profile", no_op)
    monkeypatch.setattr(team_chat, "session_run_lock", unlocked)
    monkeypatch.setattr(team_chat, "_workforce_dispatch_receipt", no_dispatch)
    monkeypatch.setattr(team_chat, "_workforce_status_receipt", lambda *_args, **_kwargs: no_op())
    monkeypatch.setattr(team_chat, "run_agent", fake_run_agent)
    monkeypatch.setattr(team_chat, "AsyncSessionLocal", lambda: db)

    async def quota(*_args):
        return SimpleNamespace(allowed=True)

    monkeypatch.setattr(team_chat, "check_agent_quota", quota)
    result = await team_chat.send(
        room_id, ChatSend(content="Tim, buat slogan acara."), workspace_id=None, db=db,
        principal=WorkforcePrincipal(owner_user_id=owner_id),
    )

    assert [agent_id for agent_id, _ in called] == [manager, researcher, writer, writer, manager, researcher]
    assert "Riset Harga to @Penulis Konten" in called[2][1]
    assert "No background work will continue" in called[3][1]
    assert "Penulis Konten to @Arthur" in called[4][1]
    assert len(result["replies"]) == 5
    assert "Diteruskan ke @Riset Harga." in result["replies"][0]["content"]
    assert "tidak mendapat giliran otomatis lagi" in result["replies"][-1]["content"]
    assert "Mention ini tidak membuat proses lanjutan di latar" in result["replies"][-1]["content"]
