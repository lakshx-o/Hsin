import asyncio
import datetime
import io
import json
import os
import re
import sys
import time
from typing import Union
import discord
from discord import app_commands
from discord.ext import tasks
import edge_tts
from gtts import gTTS
from gtts.lang import tts_langs

TOKEN = os.environ["DISCORD_TOKEN"]
VC_ID = int(os.environ["VC_ID"])  # right-click voice channel -> Copy Channel ID
LOG_ID = int(os.environ.get("LOG_CHANNEL_ID", VC_ID))  # defaults to the VC's own text chat

ALERT_ROLE_ID = int(os.environ.get("ALERT_ROLE_ID", 0))  # optional: role pinged when the first person joins
OWNER_ID = int(os.environ.get("OWNER_ID", 0))  # optional: your user ID, gets a DM if the bot can't reconnect
TTS_CHANNEL_ID = int(os.environ.get("TTS_CHANNEL_ID", VC_ID))  # defaults to the VC's own text chat
EDGE_VOICE = os.environ.get("TTS_FALLBACK_VOICE", "en-US-AriaNeural")  # backup voice if Google TTS fails
QUIET_START = int(os.environ.get("QUIET_START", 0))  # quiet hours in IST, 24h clock: TTS pauses from START up to END
QUIET_END = int(os.environ.get("QUIET_END", 4))
DEFAULT_LANG = os.environ.get("TTS_LANG", "en")  # Google TTS language code, e.g. en, hi, ta

intents = discord.Intents.default()
intents.message_content = True  # also enable "Message Content Intent" in the Developer Portal -> Bot
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
last_count = None
failures = 0
down = 0  # consecutive checks where the voice client existed but wasn't connected
custom_status = None  # set by /vcstatus; while set, the auto count status is paused
joined_at = {}  # member id -> join timestamp
START_TIME = time.time()

BOT_NAME = "Hsin"
FOOTER = "Made by Laksh"


KINDS = {  # embed colours
    "info": 0x5865F2, "ok": 0x57F287, "error": 0xED4245, "warn": 0xFEE75C, "gold": 0xF1C40F,
    "note": 0xFFE066, "join": 0x57F287, "leave": 0xED4245, "stream": 0x9B59B6, "camera": 0x1ABC9C, "empty": 0x95A5A6,
}


def E(text, title=None, kind="info", user=None):
    """Every message the bot sends goes out as one of these."""
    e = discord.Embed(title=title, description=text or None, color=KINDS.get(kind, KINDS["info"]),
                      timestamp=discord.utils.utcnow())
    icon = client.user.display_avatar.url if client.user else None
    e.set_author(name=BOT_NAME, icon_url=icon)
    e.set_footer(text=FOOTER, icon_url=icon)
    if user is not None:
        e.set_thumbnail(url=user.display_avatar.url)
    return e


def humans():
    ch = client.get_channel(VC_ID)
    return [m for m in ch.members if not m.bot] if ch else []


log_queue = None  # created in setup_hook
_recent = {}


def recently(key, secs=15):
    """True if this exact event already happened in the last `secs` seconds (drops flappy toggles)."""
    now = time.time()
    if now - _recent.get(key, 0) < secs:
        return True
    _recent[key] = now
    return False


async def log(text, silent=True, content=None, title=None, kind="info", user=None, fields=(), **kw):
    """Queue a log line. A worker posts them one by one, so voice events never wait on Discord."""
    if log_queue is None or (log_mode == "off" and not content):  # the first-person ping still goes out
        return
    try:
        log_queue.put_nowait((text, silent, content, title, kind, user, fields, kw))
    except asyncio.QueueFull:
        pass


async def log_worker():
    while True:
        text, silent, content, title, kind, user, fields, kw = await log_queue.get()
        try:
            ch = client.get_channel(LOG_ID)
            if ch:
                if log_mode == "full":
                    em = E(text, title, kind, user)
                    for name, value in fields:
                        em.add_field(name=name, value=value)
                else:  # compact: one short line, no picture, no timestamp
                    emoji = title.split(" ", 1)[0] if title else "•"
                    extra = " · ".join(f"{n}: {v}" for n, v in fields)
                    em = discord.Embed(description=f"{emoji} {text}" + (f" · {extra}" if extra else ""),
                                       color=KINDS.get(kind, KINDS["info"]))
                    em.set_footer(text=FOOTER)
                await ch.send(content=content, embed=em, silent=silent, **kw)
        except discord.Forbidden:
            print("Missing Send Messages permission in log channel")
        except discord.HTTPException as e:
            print("log send failed:", repr(e))
        finally:
            log_queue.task_done()
        await asyncio.sleep(0.5)  # stay well under Discord's rate limit


def auto_status(n):
    return f"🟢 {n} connected" if n else "💤 Empty"


async def set_vc_status(text):
    try:  # needs the "Set Voice Channel Status" permission
        await client.get_channel(VC_ID).edit(status=text)
        return True
    except Exception as e:  # incl. TypeError on a discord.py too old for status=; never break the main loop
        print("couldn't set VC status:", repr(e))
        return False


def owner_only(interaction: discord.Interaction):
    return bool(OWNER_ID) and interaction.user.id == OWNER_ID


async def update_activity():
    global last_count
    n = len(humans())
    if n != last_count:  # presence updates are rate limited, only send on change
        last_count = n
        await client.change_presence(
            activity=discord.Activity(type=discord.ActivityType.listening,
                                      name=f"{n} user{'s' * (n != 1)} in VC"))
        if custom_status is None:
            await set_vc_status(auto_status(n))


@tasks.loop(seconds=30)
async def keep_in_vc():
    global failures, down
    channel = client.get_channel(VC_ID)
    if channel is None:
        return
    vc = channel.guild.voice_client
    try:
        if vc is None:
            await channel.connect(self_deaf=True)
        elif not vc.is_connected():
            # discord.py is retrying on its own; don't fight it. Only reset if it stays down ~2 min.
            down += 1
            if down >= 4:
                down = 0
                await vc.disconnect(force=True)
            return
        elif vc.channel.id != VC_ID:  # got moved/dragged elsewhere
            await vc.move_to(channel)
        down = 0
        await update_activity()
        failures = 0
    except Exception as e:
        failures += 1
        print("reconnect failed:", e)
        if failures == 5 and OWNER_ID:  # alert once per outage
            try:
                owner = await client.fetch_user(OWNER_ID)
                await owner.send(embed=E(f"Failed to reconnect 5 times in a row:\n```{e}```", title="⚠️ Reconnect failed", kind="error"))
            except discord.HTTPException:
                pass


@client.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return
    name = member.display_name
    was = before.channel is not None and before.channel.id == VC_ID
    now = after.channel is not None and after.channel.id == VC_ID
    if now and not was:
        joined_at[member.id] = time.time()
        recent_joins[:] = [{"id": member.id, "name": name, "t": int(time.time())}] + [j for j in recent_joins if j["id"] != member.id]
        del recent_joins[5:]  # keep the last 5 different people
        save_settings()
        await log(f"**{name}** joined the voice channel", title="🟢 Joined", kind="join", user=member,
                  fields=[("In the VC now", str(len(humans())))])
        if len(humans()) == 1 and ALERT_ROLE_ID:  # first person in an empty VC
            await log(f"**{name}** just started the VC", content=f"<@&{ALERT_ROLE_ID}>", title="🔔 VC started",
                      kind="warn", user=member, silent=False, allowed_mentions=discord.AllowedMentions(roles=True))
    elif was and not now:
        t = joined_at.pop(member.id, None)
        if t:
            vc_seconds[member.id] = vc_seconds.get(member.id, 0) + (time.time() - t)
            save_settings()
        fields = []
        if t:
            mins = int((time.time() - t) // 60)
            fields.append(("Time in the VC", f"{mins} min" if mins else "under a minute"))
        fields.append(("Still in the VC", str(len(humans()))))
        await log(f"**{name}** left the voice channel", title="🔴 Left", kind="leave", user=member, fields=fields)
        if not humans():
            await log("Everyone has left.", title="📭 VC is empty", kind="empty")
            clear_tts()
    elif was and now:
        if before.self_stream != after.self_stream and not recently((member.id, 'stream')):
            on = after.self_stream
            await log(f"**{name}** {'started' if on else 'stopped'} streaming",
                      title=f"📺 Streaming {'started' if on else 'stopped'}", kind="stream", user=member)
        if before.self_video != after.self_video and not recently((member.id, 'camera')):
            on = after.self_video
            await log(f"**{name}** turned their camera {'on' if on else 'off'}",
                      title=f"📷 Camera {'on' if on else 'off'}", kind="camera", user=member)
    await update_activity()


@tree.command(description="Who is in the voice channel right now")
async def who(interaction: discord.Interaction):
    now = time.time()
    lines = [f"• {m.display_name} ({int((now - joined_at.get(m.id, now)) // 60)} min)"
             for m in humans()]
    await interaction.response.send_message(embed=E("\n".join(lines) or "Nobody's in the VC.", title="🎙️ In the voice channel", kind="join"))


@tree.command(description="Make the bot reconnect to the voice channel")
@app_commands.check(owner_only)
async def rejoin(interaction: discord.Interaction):
    await interaction.response.send_message(embed=E("Trying to reconnect to the voice channel…", title="🔄 Rejoin", kind="ok"), ephemeral=True)
    vc = interaction.guild.voice_client
    if vc:
        await vc.disconnect(force=True)  # keep_in_vc rejoins within 30s


@tree.command(description="Bot status, uptime and TTS info")
async def status(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    where = vc.channel.name if vc and vc.is_connected() else "not connected"
    up = int(time.time() - START_TIME)
    d, rem = divmod(up, 86400)
    h, rem = divmod(rem, 3600)
    uptime = (f"{d}d " if d else "") + f"{h}h {rem // 60}m"
    em = E("", title="📊 Status")
    em.description = None
    em.add_field(name="Voice", value=where)
    em.add_field(name="Latency", value=latency_text())
    em.add_field(name="Uptime", value=uptime)
    em.add_field(name="TTS", value=("off" if not tts_enabled else "paused (quiet hours)" if in_quiet_hours() else "on"))
    em.add_field(name="Queued", value=str(tts_queue.qsize() if tts_queue else 0))
    await interaction.response.send_message(embed=em)


vcstatus_last = {}  # user id -> time of their last /vcstatus (30s cooldown for non-owners)


def _err(msg, title="📝 VC status"):
    return E(msg, title=title, kind="error")


@tree.command(description="Set the VC status (leave empty to go back to the auto count)")
async def vcstatus(interaction: discord.Interaction, text: app_commands.Range[str, 1, 500] = None):
    global custom_status
    if not owner_only(interaction):  # everyone can use it, with a few guard rails
        v = getattr(interaction.user, "voice", None)
        if not v or not v.channel or v.channel.id != VC_ID:
            return await interaction.response.send_message(embed=_err("Join the voice channel first."), ephemeral=True)
        if interaction.user.id in blocked:
            return await interaction.response.send_message(embed=_err("You can't change the status."), ephemeral=True)
        if text and len(text) > 100:
            return await interaction.response.send_message(embed=_err("Keep it under 100 characters."), ephemeral=True)
        if text and apply_filter(text) != text:
            return await interaction.response.send_message(embed=_err("That text isn't allowed."), ephemeral=True)
        wait = 30 - (time.time() - vcstatus_last.get(interaction.user.id, 0))
        if wait > 0:
            return await interaction.response.send_message(embed=_err(f"Try again in {int(wait) + 1}s."), ephemeral=True)
        vcstatus_last[interaction.user.id] = time.time()
    custom_status = text[:500] if text else None  # Discord's status limit is 500 chars
    ok = await set_vc_status(custom_status or auto_status(len(humans())))
    msg = "Status updated." if ok else "Couldn't set it. Check the bot's Set Voice Channel Status permission."
    await interaction.response.send_message(embed=E(msg, title="📝 VC status", kind="ok" if ok else "error"), ephemeral=True)
    if ok:  # leave a trail of who changed it
        what = f"set the VC status to: {text}" if text else "reset the VC status to the auto count"
        await log(f"**{interaction.user.display_name}** {what}", title="📝 VC status changed", user=interaction.user)


# ---------------------------------------------------------------- TTS ----
TTS_MAX_CHARS = 300
DATA_DIR = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", ".")  # attach a Railway volume so settings survive redeploys
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
tts_channel_id = TTS_CHANNEL_ID
tts_queue = None  # created in setup_hook
tts_enabled = False  # off until someone runs /tts on (then remembered)
LANGS = tts_langs()  # {code: name}
edge_voices = []
last_speaker = (None, 0.0)  # (user id, time) -> only say "X said" when the speaker changes
user_voices = {}    # user id -> language code
custom_words = {}   # word -> how to say it (owner-added, see /pronounce)
blocked = set()     # user ids whose messages are never read (see /ttsblock)
tts_volume = 1.0    # see /volume
vc_seconds = {}     # user id -> total seconds spent in the VC (see /top)
filtered = set()    # words read as "beep" (see /ttsfilter)
react_enabled = False  # 🔊 reaction while a message is being read (see /ttsreact)
log_keep_days = 3   # join/leave logs older than this are deleted, 0 = never (see /logkeep)
quiet_enabled = True  # pause TTS during quiet hours (see /quiethours)
log_mode = "compact"  # join/leave logs: full | compact | off (see /logstyle)
recent_joins = []   # last 5 people who joined the VC, newest first (see /snipe)
user_names = {}     # user id -> the name Hsin says for them (see /name)
stickies = {}       # channel id -> {text, title, msg_id}: sticky notes everyone sees (see /sticky)
try:
    with open(SETTINGS_FILE) as f:
        _d = json.load(f)
    user_voices = {int(k): v for k, v in _d.get("voices", {}).items()}
    custom_words = _d.get("words", {})
    blocked = {int(x) for x in _d.get("blocked", [])}
    tts_volume = float(_d.get("volume", 1.0))
    vc_seconds = {int(k): float(v) for k, v in _d.get("vc_seconds", {}).items()}
    filtered = {w.lower() for w in _d.get("filter", [])}
    react_enabled = bool(_d.get("react", False))
    log_keep_days = int(_d.get("log_keep_days", 3))
    quiet_enabled = bool(_d.get("quiet_enabled", True))
    recent_joins = list(_d.get("recent_joins", []))[:5]
    user_names = {int(k): v for k, v in _d.get("names", {}).items()}
    log_mode = _d.get("log_mode", "compact") if _d.get("log_mode") in ("full", "compact", "off") else "compact"
    stickies = {int(k): v for k, v in _d.get("stickies", {}).items()}
    tts_enabled = bool(_d.get("tts_enabled", False))
except (OSError, ValueError):
    pass


def save_settings():
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump({"tts_enabled": tts_enabled, "voices": user_voices, "words": custom_words,
                       "blocked": sorted(blocked), "volume": tts_volume,
                       "vc_seconds": vc_seconds, "filter": sorted(filtered), "react": react_enabled,
                       "log_keep_days": log_keep_days, "quiet_enabled": quiet_enabled, "stickies": stickies, "log_mode": log_mode,
                       "recent_joins": recent_joins, "names": user_names}, f)
    except OSError as e:
        print("couldn't save settings:", e)


DEFAULT_WORDS = {
    # spelled out as letters
    "gg": "G G", "ggwp": "G G W P", "glhf": "G L H F", "igl": "I G L", "mvp": "M V P", "fps": "F P S",
    "lol": "L O L", "lmao": "L M A O", "lmfao": "L M F A O", "rofl": "R O F L", "wtf": "W T F", "afk": "A F K",
    "irl": "I R L", "op": "O P", "adc": "A D C", "cpu": "C P U", "gpu": "G P U", "asap": "A S A P", "fyi": "F Y I",
    # spoken out
    "idfc": "I don't care", "idc": "I don't care", "idk": "I don't know", "ikr": "I know right", "ngl": "not gonna lie",
    "smh": "shaking my head", "imo": "in my opinion", "tbh": "to be honest", "btw": "by the way", "fr": "for real",
    "jk": "just kidding", "omg": "oh my god", "nvm": "never mind", "brb": "be right back", "gtg": "got to go",
    "g2g": "got to go", "ttyl": "talk to you later", "rn": "right now", "ez": "easy", "wp": "well played",
    "gj": "good job", "gl": "good luck", "hf": "have fun", "nt": "nice try", "ns": "nice shot", "bm": "bad manners",
    "ult": "ultimate", "pls": "please", "plz": "please", "ty": "thank you", "yw": "you're welcome", "np": "no problem",
    "gn": "good night", "gm": "good morning", "wb": "welcome back", "u": "you", "ur": "your", "k": "okay",
}
DEFAULT_WORDS.update({
    # more letters
    "ai": "A I", "pc": "P C", "vc": "V C", "dm": "D M", "tv": "T V", "hp": "H P", "mp": "M P", "xp": "X P",
    "npc": "N P C", "rpg": "R P G", "pvp": "P V P", "pve": "P V E", "mmr": "M M R", "kd": "K D", "kda": "K D A",
    "dps": "D P S", "dlc": "D L C", "aoe": "A O E", "rng": "R N G", "ign": "I G N", "eta": "E T A", "pov": "P O V",
    "bff": "B F F", "nsfw": "N S F W", "sfw": "S F W", "xd": "X D", "uwu": "U W U", "owo": "O W O", "rbx": "Roblox",
    # gaming talk
    "ggs": "good games", "gz": "congrats", "gratz": "congrats", "grats": "congrats", "rekt": "wrecked",
    "dmg": "damage", "hax": "hacks", "lfg": "looking for group", "bo1": "best of one", "bo3": "best of three",
    "bo5": "best of five", "1v1": "one versus one", "2v2": "two versus two", "3v3": "three versus three",
    "4v4": "four versus four", "5v5": "five versus five", "kekw": "kek W",
    # chat slang
    "idek": "I don't even know", "idgaf": "I don't care", "stfu": "shut up", "gtfo": "get out", "wth": "what the heck",
    "lmk": "let me know", "ik": "I know", "ong": "on god", "frfr": "for real for real", "iykyk": "if you know you know",
    "iirc": "if I remember correctly", "afaik": "as far as I know", "tbf": "to be fair", "tldr": "too long didn't read",
    "omw": "on my way", "bbl": "be back later", "hbu": "how about you", "wbu": "what about you", "wym": "what you mean",
    "wdym": "what do you mean", "wyd": "what are you doing", "hmu": "hit me up", "dw": "don't worry", "rq": "real quick",
    "ofc": "of course", "obv": "obviously", "prob": "probably", "prolly": "probably",
    "bc": "because", "cuz": "because", "tho": "though", "thru": "through", "abt": "about", "b4": "before",
    "ppl": "people", "msg": "message", "convo": "conversation", "vs": "versus", "tmrw": "tomorrow", "tn": "tonight",
    "bday": "birthday", "cya": "see you", "sry": "sorry", "thx": "thanks", "tysm": "thank you so much",
    "tyvm": "thank you very much", "wut": "what", "wat": "what",
})


IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def in_quiet_hours(now=None):
    if not quiet_enabled:
        return False
    h = (now or datetime.datetime.now(IST)).hour
    if QUIET_START <= QUIET_END:
        return QUIET_START <= h < QUIET_END
    return h >= QUIET_START or h < QUIET_END  # window that wraps past midnight


def tts_active():
    """TTS is on AND it isn't quiet hours."""
    return tts_enabled and not in_quiet_hours()


def quiet_label():
    def f(h):
        return f"{(h % 12) or 12} {'AM' if h < 12 else 'PM'}"
    return f"{f(QUIET_START)} to {f(QUIET_END)} IST"


def apply_filter(t):
    if not filtered:
        return t
    pat = re.compile(r"\b(" + "|".join(re.escape(w) for w in sorted(filtered, key=len, reverse=True)) + r")\b", re.I)
    return pat.sub("beep", t)


def apply_pronunciations(t):
    words = {**DEFAULT_WORDS, **custom_words}
    pat = re.compile(r"\b(" + "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True)) + r")\b", re.I)
    return pat.sub(lambda m: words[m.group(1).lower()], t)


def clean_text(message):
    t = message.clean_content  # mentions -> names
    t = re.sub(r"```.*?```", " code block ", t, flags=re.S)
    t = re.sub(r"https?://\S+", " link ", t)
    t = re.sub(r"<a?:(\w+):\d+>", r"\1", t)  # custom emoji -> its name
    t = re.sub(r"\s+", " ", t).strip()
    return apply_pronunciations(t)[:TTS_MAX_CHARS]


def _gtts(text, lang):
    buf = io.BytesIO()
    gTTS(text, lang=lang).write_to_fp(buf)
    return buf.getvalue()


_last_alert = {}


async def alert_owner(key, text, cooldown=300, title="⚠️ TTS alert", kind="warn"):
    """DM the owner about a problem, at most once per `cooldown` seconds per kind of problem."""
    if not OWNER_ID or time.time() - _last_alert.get(key, 0) < cooldown:
        return
    _last_alert[key] = time.time()
    try:
        owner = client.get_user(OWNER_ID) or await client.fetch_user(OWNER_ID)
        await owner.send(embed=E(text[:1500], title=title, kind=kind))
    except Exception as e:
        print("couldn't DM owner:", repr(e))


async def edge_fallback(text, lang):
    global edge_voices
    voice = EDGE_VOICE
    if lang != DEFAULT_LANG:  # find a backup voice that matches the language
        try:
            if not edge_voices:
                edge_voices = await edge_tts.list_voices()
            voice = next((v["ShortName"] for v in edge_voices if v["Locale"].lower().startswith(lang.lower())), EDGE_VOICE)
        except Exception:
            pass
    buf = bytearray()
    async for chunk in edge_tts.Communicate(text, voice).stream():
        if chunk["type"] == "audio":
            buf += chunk["data"]
    return bytes(buf)


async def synth(text, lang):
    loop = asyncio.get_running_loop()
    if lang not in LANGS:
        lang = DEFAULT_LANG
    try:
        return await loop.run_in_executor(None, _gtts, text, lang)  # gTTS is blocking
    except Exception as e:
        print(f"gTTS failed ({e!r}), using edge-tts backup")
        await alert_owner("gtts", f"Google TTS failed, so I switched to the backup voice.\n```{e!r}```")
        return await edge_fallback(text, lang)


def clear_tts(stop_playing=True):
    """Drop everything queued and optionally stop what's playing."""
    while tts_queue is not None and not tts_queue.empty():
        tts_queue.get_nowait()
        tts_queue.task_done()
    if stop_playing:
        ch = client.get_channel(VC_ID)
        vc = ch.guild.voice_client if ch else None
        if vc and vc.is_playing():
            vc.stop()


async def tts_worker():
    loop = asyncio.get_running_loop()
    while True:
        user_id, text, msg = await tts_queue.get()
        try:
            ch = client.get_channel(VC_ID)
            vc = ch.guild.voice_client if ch else None
            if not vc or not vc.is_connected() or not tts_active():
                continue  # also drops anything still queued when quiet hours start
            audio = await synth(apply_filter(text), user_voices.get(user_id, DEFAULT_LANG))
            if not audio:
                continue
            done = asyncio.Event()

            def after(err):
                if err:
                    print("playback error:", err)
                loop.call_soon_threadsafe(done.set)

            source = discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(io.BytesIO(audio), pipe=True), volume=tts_volume)
            vc.play(source, after=after)
            reacted = False
            if react_enabled and msg is not None:
                try:
                    await msg.add_reaction("🔊")
                    reacted = True
                except discord.HTTPException:
                    pass
            await done.wait()
            if reacted:
                try:
                    await msg.remove_reaction("🔊", client.user)
                except discord.HTTPException:
                    pass
        except Exception as e:
            print("tts error:", repr(e))
            await alert_owner("tts", f"TTS failed and nothing was read.\n```{e!r}```", cooldown=60)
        finally:
            tts_queue.task_done()


@client.event
async def on_message(message):
    global last_speaker
    if message.guild and message.channel.id in stickies and not is_sticky_msg(message) and not is_log(message):
        schedule_sticky(message.channel)  # keeps the sticky at the bottom once chat pauses
    if not tts_active() or message.author.bot or not message.guild or not tts_channel_id or message.channel.id != tts_channel_id:
        return
    v = getattr(message.author, "voice", None)
    if not v or not v.channel or v.channel.id != VC_ID:  # only read people who are in the VC
        return
    if message.author.id in blocked:
        return
    text = clean_text(message)
    if not text or text.startswith("//"):  # "//" = don't read this
        return
    prev_id, prev_t = last_speaker
    if prev_id != message.author.id or time.time() - prev_t > 60:
        text = f"{user_names.get(message.author.id) or message.author.display_name} said {text}"
    last_speaker = (message.author.id, time.time())
    try:
        tts_queue.put_nowait((message.author.id, text, message))
    except asyncio.QueueFull:
        pass


async def lang_autocomplete(interaction: discord.Interaction, current: str):
    cur = current.lower()
    return [app_commands.Choice(name=f"{name} ({code})", value=code)
            for code, name in LANGS.items() if cur in code.lower() or cur in name.lower()][:25]


@tree.command(name="voice", description="Pick the language/accent that reads your messages")
@app_commands.autocomplete(language=lang_autocomplete)
async def voice_cmd(interaction: discord.Interaction, language: str):
    if language not in LANGS:
        return await interaction.response.send_message(embed=E("Pick one from the list.", title="Unknown language", kind="error"), ephemeral=True)
    user_voices[interaction.user.id] = language
    save_settings()
    await interaction.response.send_message(embed=E(f"Your messages will be read in **{LANGS[language]}**.", title="🗣️ Language set", kind="ok"), ephemeral=True)


@tree.command(name="tts", description="Turn reading messages aloud on or off")
@app_commands.choices(state=[app_commands.Choice(name="on", value="on"), app_commands.Choice(name="off", value="off")])
async def tts_toggle(interaction: discord.Interaction, state: app_commands.Choice[str]):
    global tts_enabled
    tts_enabled = state.value == "on"
    save_settings()
    if not tts_enabled:  # stop what's playing and drop the queue
        while not tts_queue.empty():
            tts_queue.get_nowait()
            tts_queue.task_done()
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.stop()
    await interaction.response.send_message(embed=E(f"TTS is now **{state.value}**." + (f" Quiet hours ({quiet_label()}) are active right now, so nothing is read until they end." if tts_enabled and in_quiet_hours() else ""), title="🔊 TTS", kind="ok" if tts_enabled else "warn"))


ChannelArg = Union[discord.TextChannel, discord.VoiceChannel, discord.Thread]


async def send_to(interaction, channel, **kw):
    target = interaction.channel if channel is None else interaction.guild.get_channel_or_thread(channel.id)
    if target is None:
        return await interaction.response.send_message(embed=E("I can't see that channel.", title="Can't send", kind="error"), ephemeral=True)
    try:
        await target.send(allowed_mentions=discord.AllowedMentions.none(), **kw)
    except discord.HTTPException as e:
        return await interaction.response.send_message(embed=E(f"{e}", title="Couldn't send", kind="error"), ephemeral=True)
    where = f" in {target.mention}" if channel else ""
    await interaction.response.send_message(embed=E(f"Message delivered{where}.", title="✅ Sent", kind="ok"), ephemeral=True)


@tree.command(description="Make the bot send a message (owner only)")
@app_commands.check(owner_only)
@app_commands.choices(style=[app_commands.Choice(name="plain", value="plain"), app_commands.Choice(name="embed", value="embed")])
async def say(interaction: discord.Interaction, text: app_commands.Range[str, 1, 2000],
              style: app_commands.Choice[str] = None, channel: ChannelArg = None):
    text = text.replace("\\n", "\n")
    if style and style.value == "embed":
        await send_to(interaction, channel, embed=E(text))
    else:
        await send_to(interaction, channel, content=text)


@tree.command(name="embed", description="Send a custom embed (owner only)")
@app_commands.check(owner_only)
async def embed_cmd(interaction: discord.Interaction, description: app_commands.Range[str, 1, 2000],
                    title: app_commands.Range[str, 1, 200] = None, color: str = None, channel: ChannelArg = None):
    em = E(description.replace("\\n", "\n"), title)
    if color:
        c = color.strip().lstrip("#")
        if not re.fullmatch(r"[0-9a-fA-F]{6}", c):
            return await interaction.response.send_message(embed=E("Use hex, like ff0000.", title="Invalid color", kind="error"), ephemeral=True)
        em.color = discord.Color(int(c, 16))
    await send_to(interaction, channel, embed=em)


@tree.command(description="Set how a word is read aloud (owner only); leave spoken empty to remove")
@app_commands.check(owner_only)
async def pronounce(interaction: discord.Interaction, word: app_commands.Range[str, 1, 30],
                    spoken: app_commands.Range[str, 1, 60] = None):
    w = word.lower().strip()
    if spoken:
        custom_words[w] = spoken
        msg = f"**{w}** will now be read as “{spoken}”."
    elif custom_words.pop(w, None) is not None:
        msg = f"Removed the custom pronunciation for **{w}**."
    else:
        msg = f"No custom pronunciation set for **{w}**."
    save_settings()
    await interaction.response.send_message(embed=E(msg, title="🗣️ Pronunciation", kind="ok"), ephemeral=True)


@tree.command(description="Stop (or resume) reading one person's messages (owner only)")
@app_commands.check(owner_only)
async def ttsblock(interaction: discord.Interaction, user: discord.Member):
    if user.id in blocked:
        blocked.discard(user.id)
        msg = f"**{user.display_name}** will be read again."
    else:
        blocked.add(user.id)
        msg = f"**{user.display_name}** won't be read aloud anymore."
    save_settings()
    await interaction.response.send_message(embed=E(msg, title="🚫 TTS block", kind="ok"), ephemeral=True)


@tree.command(description="Set how loud the bot speaks, 10-200% (owner only)")
@app_commands.check(owner_only)
async def volume(interaction: discord.Interaction, percent: app_commands.Range[int, 10, 200]):
    global tts_volume
    tts_volume = percent / 100
    save_settings()
    note = " (above 100% can sound distorted)" if percent > 100 else ""
    await interaction.response.send_message(embed=E(f"Volume is now **{percent}%**{note}. Applies from the next message.", title="🔊 Volume", kind="ok"), ephemeral=True)


@tree.command(description="List what the bot can do")
async def help(interaction: discord.Interaction):
    em = E("Hsin reads the VC chat aloud for people in the voice channel.", title="📖 Help")
    em.add_field(name="Everyone", inline=False, value=(
        "`/tts on|off` turn reading aloud on or off\n"
        "`/voice` pick your own language\n"
        "`/name` set the name Hsin says for you\n"
        "`/skip` skip the message being read\n"
        "`/clearqueue` stop and clear the queue\n"
        "`/who` who's in the VC\n"
        "`/top` who spent the most time in the VC\n"
        "`/snipe` the last 5 people to join the VC\n"
        "`/status` bot status\n"
        "`/info` about the bot\n"
        "`/settings` what is on and off\n"
        "`/vcstatus` set the VC status (you must be in the VC)\n"
        "`/privacy` what data the bot uses\n"
        "`/forgetme` delete your saved data\n"
        "Start a message with `//` and it won't be read."))
    await interaction.response.send_message(embed=em)  # everyone sees this one
    if owner_only(interaction):  # the owner also gets a private list of owner commands
        owner_em = E("", title="🔒 Owner commands (only you see this)", kind="note")
        owner_em.add_field(name="Owner only", inline=False, value=(
            "`/say` send a message (plain or embed)\n"
            "`/embed` send a custom embed\n"
            "`/purge` delete recent messages\n"
            "`/pronounce` change how a word is read\n"
            "`/ttsblock` stop reading one person\n"
            "`/volume` change the speaking volume\n"
            "`/ttsfilter` words read as \"beep\"\n"
            "`/ttsreact` 🔊 reaction while reading\n"
            "`/logkeep` auto-delete old join/leave logs\n"
            "`/logstyle` full, compact or off\n"
            "`/quiethours` pause TTS at night\n"
            "`/sticky` sticky notes everyone sees (set, remove, list)\n"
            "`/rejoin` reconnect to the VC\n"
            "`/ttschannel` read this channel until restart"))
        await interaction.followup.send(embed=owner_em, ephemeral=True)


@tree.command(description="Who has spent the most time in the VC")
async def top(interaction: discord.Interaction):
    now = time.time()
    totals = dict(vc_seconds)
    for uid, t in joined_at.items():  # include people currently in the VC
        totals[uid] = totals.get(uid, 0) + (now - t)
    if not totals:
        return await interaction.response.send_message(embed=E("Nothing tracked yet.", title="🏆 VC time leaderboard", kind="warn"), ephemeral=True)
    lines = []
    for i, (uid, secs) in enumerate(sorted(totals.items(), key=lambda x: -x[1])[:10], 1):
        h, m = divmod(int(secs) // 60, 60)
        lines.append(f"**{i}.** <@{uid}> — {h}h {m}m")
    await interaction.response.send_message(embed=E("\n".join(lines), title="🏆 VC time leaderboard", kind="gold"))


@tree.command(description="Words that get read as 'beep' (owner only)")
@app_commands.check(owner_only)
@app_commands.choices(action=[app_commands.Choice(name=n, value=n) for n in ("add", "remove", "list")])
async def ttsfilter(interaction: discord.Interaction, action: app_commands.Choice[str],
                    word: app_commands.Range[str, 1, 30] = None):
    if action.value == "list":
        msg = ", ".join(sorted(filtered)) or "No filtered words."
    elif not word:
        msg = "Give a word to add or remove."
    else:
        w = word.lower().strip()
        if action.value == "add":
            filtered.add(w)
            msg = f"**{w}** will be read as “beep”."
        else:
            filtered.discard(w)
            msg = f"**{w}** removed from the filter."
        save_settings()
    await interaction.response.send_message(embed=E(msg, title="🤬 TTS filter", kind="ok"), ephemeral=True)


@tree.command(description="Show a 🔊 reaction on messages while they're read (owner only)")
@app_commands.check(owner_only)
@app_commands.choices(state=[app_commands.Choice(name="on", value="on"), app_commands.Choice(name="off", value="off")])
async def ttsreact(interaction: discord.Interaction, state: app_commands.Choice[str]):
    global react_enabled
    react_enabled = state.value == "on"
    save_settings()
    await interaction.response.send_message(embed=E(f"Reactions are now **{state.value}**.", title="🔊 Reactions", kind="ok"), ephemeral=True)


@tree.command(description="Delete old join/leave logs after N days, 0 = never (owner only)")
@app_commands.check(owner_only)
async def logkeep(interaction: discord.Interaction, days: app_commands.Range[int, 0, 30]):
    global log_keep_days
    log_keep_days = days
    save_settings()
    msg = f"Join/leave logs older than **{days} day{'s' * (days != 1)}** will be deleted." if days else "Log cleanup is **off**."
    await interaction.response.send_message(embed=E(msg, title="🧹 Log cleanup", kind="ok"), ephemeral=True)


@tree.command(description="Pause TTS during quiet hours on or off (owner only)")
@app_commands.check(owner_only)
@app_commands.choices(state=[app_commands.Choice(name="on", value="on"), app_commands.Choice(name="off", value="off")])
async def quiethours(interaction: discord.Interaction, state: app_commands.Choice[str]):
    global quiet_enabled
    quiet_enabled = state.value == "on"
    save_settings()
    msg = f"🌙 Quiet hours are **on**: nothing is read {quiet_label()}, then it resumes by itself." if quiet_enabled else "🌙 Quiet hours are **off**."
    await interaction.response.send_message(embed=E(msg, title="🌙 Quiet hours", kind="ok"), ephemeral=True)


sticky_tasks = {}  # channel id -> running repost task
sticky_last = {}   # channel id -> time of the latest message there


def is_sticky_msg(m):
    return m.author.id == client.user.id and bool(m.embeds) and (m.embeds[0].title or "").startswith("📌")


def sticky_embed(data):
    return E(data["text"].replace("\\n", "\n"), title=f"📌 {data.get('title') or 'Sticky note'}", kind="note")


async def post_sticky(channel):
    """Post the sticky at the bottom, then remove the old copy."""
    data = stickies.get(channel.id)
    if not data:
        return
    old = data.get("msg_id")
    try:
        new = await channel.send(embed=sticky_embed(data))
    except discord.HTTPException as e:
        print("sticky post failed:", repr(e))
        return
    data["msg_id"] = new.id
    save_settings()
    if old:
        try:
            await channel.get_partial_message(old).delete()
        except discord.HTTPException:
            pass


async def _sticky_worker(channel):
    try:
        while True:
            while time.time() - sticky_last[channel.id] < 4:  # wait for the chat to pause
                await asyncio.sleep(1)
            started = time.time()
            await post_sticky(channel)
            if sticky_last[channel.id] <= started:
                break
    finally:
        sticky_tasks.pop(channel.id, None)


def schedule_sticky(channel):
    sticky_last[channel.id] = time.time()
    if channel.id not in sticky_tasks:
        sticky_tasks[channel.id] = asyncio.create_task(_sticky_worker(channel))


sticky = app_commands.Group(name="sticky", description="Sticky notes that stay at the bottom of a channel (owner only)")
tree.add_command(sticky)


@sticky.command(name="set", description="Create or update the sticky note in a channel")
@app_commands.check(owner_only)
async def sticky_set(interaction: discord.Interaction, text: app_commands.Range[str, 1, 1500],
                     title: app_commands.Range[str, 1, 100] = None, channel: ChannelArg = None):
    target = interaction.channel if channel is None else interaction.guild.get_channel_or_thread(channel.id)
    if target is None:
        return await interaction.response.send_message(embed=E("I can't see that channel.", title="Can't set sticky", kind="error"), ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    old = stickies.get(target.id, {}).get("msg_id")
    stickies[target.id] = {"text": text, "title": title, "msg_id": None}
    await post_sticky(target)
    if old:
        try:
            await target.get_partial_message(old).delete()
        except discord.HTTPException:
            pass
    ok = stickies[target.id].get("msg_id") is not None
    await interaction.followup.send(embed=E(
        f"Sticky is live in {target.mention} and will stay at the bottom." if ok else "I couldn't post there. Check my permissions.",
        title="📌 Sticky set" if ok else "Couldn't post", kind="ok" if ok else "error"), ephemeral=True)


@sticky.command(name="remove", description="Remove the sticky note from a channel")
@app_commands.check(owner_only)
async def sticky_remove(interaction: discord.Interaction, channel: ChannelArg = None):
    target = interaction.channel if channel is None else interaction.guild.get_channel_or_thread(channel.id)
    data = stickies.pop(target.id, None) if target else None
    if not data:
        return await interaction.response.send_message(embed=E("There's no sticky there.", title="Nothing to remove", kind="warn"), ephemeral=True)
    save_settings()
    if data.get("msg_id"):
        try:
            await target.get_partial_message(data["msg_id"]).delete()
        except discord.HTTPException:
            pass
    await interaction.response.send_message(embed=E(f"Removed from {target.mention}.", title="🗑️ Sticky removed", kind="ok"), ephemeral=True)


@sticky.command(name="list", description="Show where stickies are set")
@app_commands.check(owner_only)
async def sticky_list(interaction: discord.Interaction):
    if not stickies:
        return await interaction.response.send_message(embed=E("None yet. Use `/sticky set`.", title="📌 Stickies", kind="note"), ephemeral=True)
    lines = [f"<#{cid}> — {d['text'][:60]}{'…' if len(d['text']) > 60 else ''}" for cid, d in stickies.items()]
    await interaction.response.send_message(embed=E("\n".join(lines), title="📌 Stickies", kind="note"), ephemeral=True)


PRIVACY_TEXT = (
    "Messages sent in the VC chat while you're in the VC are read aloud, so their text is sent to Google (or Microsoft if Google is down) to make the speech. Hsin doesn't store message text. It saves only your user ID, your chosen language and spoken name, and your total time in the VC.\nStart a message with `//` and it won't be read. `/forgetme` deletes your saved data.")


@tree.command(description="What data the bot uses and how to opt out")
async def privacy(interaction: discord.Interaction):
    await interaction.response.send_message(embed=E(PRIVACY_TEXT, title="🔒 Privacy", kind="info"), ephemeral=True)


@tree.command(description="Delete the data the bot saved about you")
async def forgetme(interaction: discord.Interaction):
    had = user_voices.pop(interaction.user.id, None) is not None
    had = (vc_seconds.pop(interaction.user.id, None) is not None) or had
    had = (user_names.pop(interaction.user.id, None) is not None) or had
    save_settings()
    msg = "Your saved language, name and VC time were deleted." if had else "There was nothing saved about you."
    await interaction.response.send_message(embed=E(msg, title="🧹 Done", kind="ok"), ephemeral=True)


@tree.command(description="Change how join/leave logs look: full, compact or off (owner only)")
@app_commands.check(owner_only)
@app_commands.choices(mode=[app_commands.Choice(name=n, value=n) for n in ("compact", "full", "off")])
async def logstyle(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    global log_mode
    log_mode = mode.value
    save_settings()
    desc = {"compact": "One short line per event, no pictures. Lightest on the chat.",
            "full": "Titles, avatars and details, like before.",
            "off": "No join/leave logs at all."}[log_mode]
    await interaction.response.send_message(embed=E(desc, title=f"🧾 Log style: {log_mode}", kind="ok"), ephemeral=True)


@tree.command(description="Delete recent messages in a channel (owner only)")
@app_commands.check(owner_only)
async def purge(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100],
                user: discord.Member = None, channel: ChannelArg = None):
    target = interaction.channel if channel is None else interaction.guild.get_channel_or_thread(channel.id)
    if target is None:
        return await interaction.response.send_message(embed=E("I can't see that channel.", title="Can't purge", kind="error"), ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    count = 0

    def check(m):
        nonlocal count
        if m.pinned or is_sticky_msg(m) or count >= amount:  # never touch pins or the sticky note
            return False
        if user and m.author.id != user.id:
            return False
        count += 1
        return True

    try:
        deleted = await target.purge(limit=500 if user else amount, check=check, bulk=True)
    except discord.Forbidden:
        return await interaction.followup.send(embed=E("I need **Manage Messages** and **Read Message History** there.",
                                                        title="Missing permission", kind="error"), ephemeral=True)
    except discord.HTTPException as e:
        return await interaction.followup.send(embed=E(f"{e}", title="Purge failed", kind="error"), ephemeral=True)
    who = f" from **{user.display_name}**" if user else ""
    await interaction.followup.send(embed=E(f"Deleted **{len(deleted)}** message{'s' * (len(deleted) != 1)}{who} in {target.mention}.",
                                            title="🧹 Purged", kind="ok"), ephemeral=True)
    await log(f"**{interaction.user.display_name}** purged {len(deleted)} message{'s' * (len(deleted) != 1)}{who} in {target.mention}",
              title="🧹 Purge", kind="warn", user=interaction.user)


@tree.command(description="Show the last 5 people who joined the VC")
async def snipe(interaction: discord.Interaction):
    if not recent_joins:
        return await interaction.response.send_message(
            embed=E("Nobody has joined since I started tracking.", title="🎯 Snipe", kind="warn"), ephemeral=True)
    lines = [f"**{i}.** <@{j['id']}> — <t:{j['t']}:R>" + (" 🟢" if j["id"] in joined_at else "")
             for i, j in enumerate(recent_joins, 1)]
    await interaction.response.send_message(
        embed=E("\n".join(lines) + "\n\n🟢 = still in the VC", title="🎯 Last 5 to join the VC", kind="join"))


@tree.command(description="Show which features are on and off")
async def settings(interaction: discord.Interaction):
    def onoff(flag, extra=""):
        return ("✅ on" if flag else "❌ off") + (f" {extra}" if extra and flag else "")

    em = E("", title="⚙️ Settings", kind="info")
    em.description = None
    tts_note = "(paused, quiet hours)" if tts_enabled and in_quiet_hours() else ""
    fields = [
        ("🔊 TTS", onoff(tts_enabled) + (f" {tts_note}" if tts_note else "")),
        ("🌙 Quiet hours", onoff(quiet_enabled, f"({quiet_label()})")),
        ("🔊 Reactions", onoff(react_enabled)),
        ("🔈 Volume", f"{round(tts_volume * 100)}%"),
        ("🗣️ Your language", LANGS.get(user_voices.get(interaction.user.id, DEFAULT_LANG), DEFAULT_LANG)),
        ("🏷️ Your spoken name", user_names.get(interaction.user.id) or "your Discord name"),
        ("📢 TTS channel", f"<#{tts_channel_id}>" if tts_channel_id else "❌ none"),
        ("🧾 Join/leave logs", onoff(log_mode != "off", f"({log_mode})")),
        ("🧹 Log cleanup", onoff(bool(log_keep_days), f"(after {log_keep_days} day{'s' * (log_keep_days != 1)})")),
        ("📝 VC status", "custom" if custom_status else "auto (user count)"),
        ("📌 Stickies", str(len(stickies))),
        ("🚫 Blocked from TTS", str(len(blocked))),
        ("🤬 Filtered words", str(len(filtered))),
        ("🗣️ Custom pronunciations", str(len(custom_words))),
    ]
    for name, value in fields:
        em.add_field(name=name, value=value)
    await interaction.response.send_message(embed=em)


def latency_text():
    lat = client.latency
    return "n/a" if lat != lat else f"{round(lat * 1000)}ms"  # NaN while not connected


def uptime_text():
    up = int(time.time() - START_TIME)
    d, rem = divmod(up, 86400)
    h, rem = divmod(rem, 3600)
    return (f"{d}d " if d else "") + f"{h}h {rem // 60}m"


@tree.command(description="About the bot")
async def info(interaction: discord.Interaction):
    ch = client.get_channel(VC_ID)
    voice = f"{ch.name} ({len(humans())} in it)" if ch else "not found"
    em = E("A voice channel sitter that reads the VC chat aloud.", title=f"🤖 About {BOT_NAME}", user=client.user)
    n_cmds = sum(1 for _ in tree.walk_commands())
    py = sys.version_info
    fields = [
        ("👤 Made by", "Laksh"),
        ("⏱️ Uptime", uptime_text()),
        ("📡 Latency", latency_text()),
        ("🎙️ Voice channel", voice),
        ("🔊 TTS", f"Google TTS, with a Microsoft backup voice\n{len(LANGS)} languages"),
        ("🧩 Commands", str(n_cmds)),
        ("📚 Pronunciations", f"{len(DEFAULT_WORDS) + len(custom_words)} words"),
        ("🐍 Built with", f"Python {py.major}.{py.minor} · discord.py {discord.__version__}"),
    ]
    for name, value in fields:
        em.add_field(name=name, value=value)
    await interaction.response.send_message(embed=em)


@tree.command(name="name", description="Set the name Hsin says when reading your messages (leave empty to reset)")
async def name_cmd(interaction: discord.Interaction, name: app_commands.Range[str, 1, 32] = None):
    if name is None:
        had = user_names.pop(interaction.user.id, None) is not None
        save_settings()
        msg = "Back to your Discord name." if had else "You didn't have a custom name."
        return await interaction.response.send_message(embed=E(msg, title="🏷️ Name reset", kind="ok"), ephemeral=True)
    name = re.sub(r"\s+", " ", name).strip()
    if not name or apply_filter(name) != name:
        return await interaction.response.send_message(
            embed=E("Pick a different name.", title="Name not allowed", kind="error"), ephemeral=True)
    user_names[interaction.user.id] = name
    save_settings()
    await interaction.response.send_message(
        embed=E(f"Hsin will say “{name} said …” when it reads your messages.", title="🏷️ Name set", kind="ok"), ephemeral=True)


@tree.command(description="Skip the message being read right now")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()
        await interaction.response.send_message(embed=E("Moving on to the next message.", title="⏭️ Skipped", kind="ok"))
    else:
        await interaction.response.send_message(embed=E("There is nothing to skip right now.", title="Nothing playing", kind="warn"), ephemeral=True)


@tree.command(description="Stop reading and clear everything queued")
async def clearqueue(interaction: discord.Interaction):
    while not tts_queue.empty():
        tts_queue.get_nowait()
        tts_queue.task_done()
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()
    await interaction.response.send_message(embed=E("Stopped reading and cleared the queue.", title="🧹 Queue cleared", kind="ok"))


@tree.command(description="Read messages from THIS channel aloud (until restart; set TTS_CHANNEL_ID to keep it)")
@app_commands.check(owner_only)
async def ttschannel(interaction: discord.Interaction):
    global tts_channel_id
    tts_channel_id = interaction.channel_id
    await interaction.response.send_message(embed=E(f"Now reading <#{tts_channel_id}> for people in the VC.", title="📢 TTS channel", kind="ok"))
# ---------------------------------------------------------------------------


@tree.error
async def on_command_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message(embed=E("Only the bot owner can use this.", title="Owner only", kind="error"), ephemeral=True)
    else:
        raise error


LOG_MARKS = ("🟢", "🔴", "📭", "📺", "📷", "🔔")


def is_log(m):
    """Only the bot's own join/leave/stream logs - never /say or /embed posts."""
    if m.author.id != client.user.id or not m.embeds:
        return False
    title, d = m.embeds[0].title or "", m.embeds[0].description or ""
    return title.startswith(LOG_MARKS) or d.startswith(LOG_MARKS) or d.endswith("just started the VC")


@tasks.loop(hours=1)
async def clean_logs():
    if not log_keep_days:
        return
    ch = client.get_channel(LOG_ID)
    if ch is None:
        return
    cutoff = discord.utils.utcnow() - datetime.timedelta(days=log_keep_days)
    deleted = 0
    try:
        async for m in ch.history(limit=None, before=cutoff, after=cutoff - datetime.timedelta(days=7)):
            if is_log(m):
                await m.delete()
                deleted += 1
                if deleted >= 200:  # be gentle, the next run continues
                    break
    except discord.HTTPException as e:
        print("log cleanup failed:", repr(e))
    if deleted:
        print(f"log cleanup: deleted {deleted} old logs")


@client.event
async def setup_hook():
    global tts_queue, log_queue
    tts_queue = asyncio.Queue(maxsize=20)
    log_queue = asyncio.Queue(maxsize=50)
    client.log_task = asyncio.create_task(log_worker())
    client.tts_task = asyncio.create_task(tts_worker())


@client.event
async def on_ready():
    print(f"Logged in as {client}")
    ch = client.get_channel(VC_ID)
    if ch and not getattr(client, "synced", False):  # single server: guild sync shows up instantly
        client.synced = True
        try:
            tree.copy_global_to(guild=ch.guild)
            synced = await tree.sync(guild=ch.guild)
            print(f"Synced {len(synced)} commands to {ch.guild.name}: {[c.name for c in synced]}")
            tree.clear_commands(guild=None)  # remove old global copies so commands don't show twice
            await tree.sync()
            await alert_owner("startup", f"Online and in **{ch.guild.name}**.\nSynced **{len(synced)}** commands.\n"
                              f"TTS is **{'on' if tts_enabled else 'off'}**.", cooldown=0, title="✅ Hsin is online", kind="ok")
        except Exception as e:
            print("Command sync failed:", repr(e))
            await alert_owner("startup", f"Started, but command sync failed:\n```{e!r}```", cooldown=0,
                              title="⚠️ Hsin started with a problem")
    elif not ch:
        print("Can't see VC_ID channel - check VC_ID and that the bot is in that server")
        await alert_owner("startup", "Started, but I can't see the VC_ID channel. Check `VC_ID` and that I'm in that server.",
                          cooldown=0, title="⚠️ Hsin started with a problem")
    for m in humans():  # people already in VC when the bot (re)starts
        joined_at.setdefault(m.id, time.time())
    if not keep_in_vc.is_running():
        keep_in_vc.start()
    if not clean_logs.is_running():
        clean_logs.start()


client.run(TOKEN)

