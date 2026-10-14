import os
import time
import discord
from discord import app_commands
from discord.ext import tasks

TOKEN = os.environ["DISCORD_TOKEN"]
VC_ID = int(os.environ["VC_ID"])  # right-click voice channel -> Copy Channel ID
LOG_ID = int(os.environ.get("LOG_CHANNEL_ID", VC_ID))  # defaults to the VC's own text chat

ALERT_ROLE_ID = int(os.environ.get("ALERT_ROLE_ID", 0))  # optional: role pinged when the first person joins
OWNER_ID = int(os.environ.get("OWNER_ID", 0))  # optional: your user ID, gets a DM if the bot can't reconnect

client = discord.Client(intents=discord.Intents.default())
tree = app_commands.CommandTree(client)
last_count = None
failures = 0
custom_status = None  # set by /vcstatus; while set, the auto count status is paused
joined_at = {}  # member id -> join timestamp


def humans():
    ch = client.get_channel(VC_ID)
    return [m for m in ch.members if not m.bot] if ch else []


async def log(text, silent=True, **kw):
    ch = client.get_channel(LOG_ID)
    if ch:
        try:
            await ch.send(text, silent=silent, **kw)
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
    global failures
    channel = client.get_channel(VC_ID)
    if channel is None:
        return
    vc = channel.guild.voice_client
    try:
        if vc is None or not vc.is_connected():
            if vc:
                await vc.disconnect(force=True)
            await channel.connect(self_deaf=True)
        elif vc.channel.id != VC_ID:  # got moved/dragged elsewhere
            await vc.move_to(channel)
        await update_activity()
        failures = 0
    except Exception as e:
        failures += 1
        print("reconnect failed:", e)
        if failures == 5 and OWNER_ID:  # alert once per outage
            try:
                owner = await client.fetch_user(OWNER_ID)
                await owner.send(f"⚠️ VC bot failed to reconnect 5 times in a row: {e}")
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
        if len(humans()) == 1 and ALERT_ROLE_ID:  # first person in an empty VC
            await log(f"<@&{ALERT_ROLE_ID}> **{member.display_name}** just started the VC",
                      silent=False, allowed_mentions=discord.AllowedMentions(roles=True))
    elif was and not now:
        t = joined_at.pop(member.id, None)
        spent = f" after {int((time.time() - t) // 60)} min" if t else ""
        await log(f"🔴 **{member.display_name}** left{spent}")
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
    await interaction.response.send_message("\n".join(lines) or "Nobody's in the VC.")


@tree.command(description="Make the bot reconnect to the voice channel")
@app_commands.check(owner_only)
async def rejoin(interaction: discord.Interaction):
    await interaction.response.send_message("Reconnecting...", ephemeral=True)
    vc = interaction.guild.voice_client
    if vc:
        await vc.disconnect(force=True)  # keep_in_vc rejoins within 30s


@tree.command(description="Bot status and latency")
async def status(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    where = vc.channel.name if vc and vc.is_connected() else "not connected"
    await interaction.response.send_message(
        f"Voice: **{where}** | Latency: {round(client.latency * 1000)}ms")


@tree.command(description="Set a custom VC status (leave empty to go back to the auto count)")
@app_commands.check(owner_only)
async def vcstatus(interaction: discord.Interaction, text: str = None):
    global custom_status
    custom_status = text[:500] if text else None  # Discord's status limit is 500 chars
    ok = await set_vc_status(custom_status or auto_status(len(humans())))
    msg = "Status updated." if ok else "Couldn't set it. Check the bot's Set Voice Channel Status permission."
    await interaction.response.send_message(msg, ephemeral=True)


@tree.error
async def on_command_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message("Only the bot owner can use this.", ephemeral=True)
    else:
        raise error


@client.event
async def setup_hook():
    await tree.sync()


@client.event
async def on_ready():
    print(f"Logged in as {client}")
    for m in humans():  # people already in VC when the bot (re)starts
        joined_at.setdefault(m.id, time.time())
    if not keep_in_vc.is_running():
        keep_in_vc.start()


client.run(TOKEN)
