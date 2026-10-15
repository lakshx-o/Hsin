import asyncio
import io
import json
import os
import re
import time
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

BOT_NAME = "Hsin"
FOOTER = "Made by Laksh"


def E(text, title=None):
    """Every message the bot sends goes out as one of these."""
    e = discord.Embed(title=title, description=text, color=0x5865F2)
    e.set_author(name=BOT_NAME)
    e.set_footer(text=FOOTER)
    return e


def humans():
    ch = client.get_channel(VC_ID)
    return [m for m in ch.members if not m.bot] if ch else []


async def log(text, silent=True, content=None, **kw):
    ch = client.get_channel(LOG_ID)
    if ch:
        try:
            await ch.send(content=content, embed=E(text), silent=silent, **kw)
        except discord.Forbidden:
            print("Missing Send Messages permission in log channel")


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
                await owner.send(embed=E(f"⚠️ VC bot failed to reconnect 5 times in a row: {e}"))
            except discord.HTTPException:
                pass


@client.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return
    was = before.channel is not None and before.channel.id == VC_ID
    now = after.channel is not None and after.channel.id == VC_ID
    if now and not was:
        joined_at[member.id] = time.time()
        await log(f"🟢 **{member.display_name}** joined")
        announce(f"{member.display_name} joined")
        if len(humans()) == 1 and ALERT_ROLE_ID:  # first person in an empty VC
            await log(f"**{member.display_name}** just started the VC", content=f"<@&{ALERT_ROLE_ID}>",
                      silent=False, allowed_mentions=discord.AllowedMentions(roles=True))
    elif was and not now:
        t = joined_at.pop(member.id, None)
        spent = f" after {int((time.time() - t) // 60)} min" if t else ""
        await log(f"🔴 **{member.display_name}** left{spent}")
        announce(f"{member.display_name} left")
        if not humans():
            await log("📭 The VC is empty now")
    elif was and now:
        if before.self_stream != after.self_stream:
            await log(f"📺 **{member.display_name}** {'started' if after.self_stream else 'stopped'} streaming")
        if before.self_video != after.self_video:
            await log(f"📷 **{member.display_name}** turned their camera {'on' if after.self_video else 'off'}")
    await update_activity()


@tree.command(description="Who is in the voice channel right now")
async def who(interaction: discord.Interaction):
    now = time.time()
    lines = [f"• {m.display_name} ({int((now - joined_at.get(m.id, now)) // 60)} min)"
             for m in humans()]
    await interaction.response.send_message(embed=E("\n".join(lines) or "Nobody's in the VC."))


@tree.command(description="Make the bot reconnect to the voice channel")
@app_commands.check(owner_only)
async def rejoin(interaction: discord.Interaction):
    await interaction.response.send_message(embed=E("Reconnecting..."), ephemeral=True)
    vc = interaction.guild.voice_client
    if vc:
        await vc.disconnect(force=True)  # keep_in_vc rejoins within 30s


@tree.command(description="Bot status and latency")
async def status(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    where = vc.channel.name if vc and vc.is_connected() else "not connected"
    await interaction.response.send_message(
        embed=E(f"Voice: **{where}** | Latency: {round(client.latency * 1000)}ms", title="Status"))


@tree.command(description="Set a custom VC status (leave empty to go back to the auto count)")
@app_commands.check(owner_only)
async def vcstatus(interaction: discord.Interaction, text: str = None):
    global custom_status
    custom_status = text[:500] if text else None  # Discord's status limit is 500 chars
    ok = await set_vc_status(custom_status or auto_status(len(humans())))
    msg = "Status updated." if ok else "Couldn't set it. Check the bot's Set Voice Channel Status permission."
    await interaction.response.send_message(embed=E(msg), ephemeral=True)


# ---------------------------------------------------------------- TTS ----
TTS_MAX_CHARS = 300
VOICES_FILE = "voices.json"  # per-user languages; resets on hosts with ephemeral disks
tts_channel_id = TTS_CHANNEL_ID
tts_queue = None  # created in setup_hook
tts_enabled = False  # off until someone runs /tts on
LANGS = tts_langs()  # {code: name}
edge_voices = []
last_speaker = (None, 0.0)  # (user id, time) -> only say "X said" when the speaker changes
try:
    with open(VOICES_FILE) as f:
        user_voices = {int(k): v for k, v in json.load(f).items()}
except (OSError, ValueError):
    user_voices = {}


def save_voices():
    try:
        with open(VOICES_FILE, "w") as f:
            json.dump(user_voices, f)
    except OSError as e:
        print("couldn't save voices:", e)


def clean_text(message):
    t = message.clean_content  # mentions -> names
    t = re.sub(r"```.*?```", " code block ", t, flags=re.S)
    t = re.sub(r"https?://\S+", " link ", t)
    t = re.sub(r"<a?:(\w+):\d+>", r"\1", t)  # custom emoji -> its name
    t = re.sub(r"\s+", " ", t).strip()
    return t[:TTS_MAX_CHARS]


def _gtts(text, lang):
    buf = io.BytesIO()
    gTTS(text, lang=lang).write_to_fp(buf)
    return buf.getvalue()


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
        return await edge_fallback(text, lang)


def announce(text):
    """Queue a spoken line (joins/leaves) using the default language."""
    if tts_enabled and tts_queue is not None:
        try:
            tts_queue.put_nowait((0, text))
        except asyncio.QueueFull:
            pass


async def tts_worker():
    loop = asyncio.get_running_loop()
    while True:
        user_id, text = await tts_queue.get()
        try:
            ch = client.get_channel(VC_ID)
            vc = ch.guild.voice_client if ch else None
            if not vc or not vc.is_connected():
                continue
            audio = await synth(text, user_voices.get(user_id, DEFAULT_LANG))
            if not audio:
                continue
            done = asyncio.Event()

            def after(err):
                if err:
                    print("playback error:", err)
                loop.call_soon_threadsafe(done.set)

            vc.play(discord.FFmpegPCMAudio(io.BytesIO(audio), pipe=True), after=after)
            await done.wait()
        except Exception as e:
            print("tts error:", repr(e))
        finally:
            tts_queue.task_done()


@client.event
async def on_message(message):
    global last_speaker
    if not tts_enabled or message.author.bot or not message.guild or not tts_channel_id or message.channel.id != tts_channel_id:
        return
    v = getattr(message.author, "voice", None)
    if not v or not v.channel or v.channel.id != VC_ID:  # only read people who are in the VC
        return
    text = clean_text(message)
    if not text or text.startswith("//"):  # "//" = don't read this
        return
    prev_id, prev_t = last_speaker
    if prev_id != message.author.id or time.time() - prev_t > 60:
        text = f"{message.author.display_name} said {text}"
    last_speaker = (message.author.id, time.time())
    try:
        tts_queue.put_nowait((message.author.id, text))
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
        return await interaction.response.send_message(embed=E("Unknown language, pick one from the list."), ephemeral=True)
    user_voices[interaction.user.id] = language
    save_voices()
    await interaction.response.send_message(embed=E(f"Your TTS language is now **{LANGS[language]}**."), ephemeral=True)


@tree.command(name="tts", description="Turn reading messages aloud on or off")
@app_commands.choices(state=[app_commands.Choice(name="on", value="on"), app_commands.Choice(name="off", value="off")])
async def tts_toggle(interaction: discord.Interaction, state: app_commands.Choice[str]):
    global tts_enabled
    tts_enabled = state.value == "on"
    if not tts_enabled:  # stop what's playing and drop the queue
        while not tts_queue.empty():
            tts_queue.get_nowait()
            tts_queue.task_done()
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.stop()
    await interaction.response.send_message(embed=E(f"🔊 TTS is now **{state.value}**."))


@tree.command(description="Skip the message being read right now")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()
        await interaction.response.send_message(embed=E("⏭️ Skipped."))
    else:
        await interaction.response.send_message(embed=E("Nothing is playing."), ephemeral=True)


@tree.command(description="Stop reading and clear everything queued")
async def clearqueue(interaction: discord.Interaction):
    while not tts_queue.empty():
        tts_queue.get_nowait()
        tts_queue.task_done()
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()
    await interaction.response.send_message(embed=E("🧹 Queue cleared."))


@tree.command(description="Read messages from THIS channel aloud (until restart; set TTS_CHANNEL_ID to keep it)")
@app_commands.check(owner_only)
async def ttschannel(interaction: discord.Interaction):
    global tts_channel_id
    tts_channel_id = interaction.channel_id
    await interaction.response.send_message(embed=E(f"Now reading <#{tts_channel_id}> for people in the VC."))
# ---------------------------------------------------------------------------


@tree.error
async def on_command_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message(embed=E("Only the bot owner can use this."), ephemeral=True)
    else:
        raise error


@client.event
async def setup_hook():
    global tts_queue
    tts_queue = asyncio.Queue(maxsize=20)
    client.tts_task = asyncio.create_task(tts_worker())
    await tree.sync()


@client.event
async def on_ready():
    print(f"Logged in as {client}")
    for m in humans():  # people already in VC when the bot (re)starts
        joined_at.setdefault(m.id, time.time())
    if not keep_in_vc.is_running():
        keep_in_vc.start()


client.run(TOKEN)
