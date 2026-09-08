import aiohttp
import asyncio
import json
import logging
import os
import socket

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

LOCK_PORT = int(os.getenv("LOCK_PORT", "27843"))

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
WEBHOOK_URL = os.getenv("VOUCH_WEBHOOK_URL", "").strip()
STAFF_ROLE_IDS = [
    int(x)
    for x in os.getenv("STAFF_ROLE_IDS", "").split(",")
    if x.strip().isdigit()
]
STAFF_ROLE_NAMES = {
    name.strip().casefold()
    for name in os.getenv("STAFF_ROLE_NAMES", "").split(",")
    if name.strip()
}

SELLAUTH_SHOP_ID = os.getenv("SELLAUTH_SHOP_ID", "").strip()
SELLAUTH_API_KEY = os.getenv("SELLAUTH_API_KEY", "").strip()

EMOJI = {"positive": "\u2705", "neutral": "\U0001F610", "negative": "\u274C"}
LABEL = {"positive": "Positive", "neutral": "Neutral", "negative": "Negative"}
COLOR = {"positive": 0x2d5a3d, "neutral": 0x5c4a1a, "negative": 0x5c1a1a}

STAFF_VOUCHES_FILE = os.path.join(BASE_DIR, "staff_vouches.json")
PRODUCT_VOUCHES_FILE = os.path.join(BASE_DIR, "product_vouches.json")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(os.path.join(BASE_DIR, "bot.log"), encoding="utf-8")],
)

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


def load_json(path: str) -> dict:
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_json(path: str, data: dict):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def load_config() -> dict:
    return load_json(CONFIG_FILE)


def save_config(data: dict):
    save_json(CONFIG_FILE, data)


def load_staff_vouches() -> dict:
    return load_json(STAFF_VOUCHES_FILE)


def save_staff_vouches(data: dict):
    save_json(STAFF_VOUCHES_FILE, data)


def load_product_vouches() -> dict:
    return load_json(PRODUCT_VOUCHES_FILE)


def save_product_vouches(data: dict):
    save_json(PRODUCT_VOUCHES_FILE, data)


def is_staff(member: discord.Member) -> bool:
    if STAFF_ROLE_IDS or STAFF_ROLE_NAMES:
        if any(r.id in STAFF_ROLE_IDS for r in member.roles):
            return True
        return any(r.name.casefold() in STAFF_ROLE_NAMES for r in member.roles)
    return member.guild_permissions.kick_members


def is_admin(member: discord.Member) -> bool:
    return member.guild_permissions.manage_guild


def staff_role_name(member: discord.Member) -> str:
    top = getattr(member, "top_role", None)
    if top and top.id != member.guild.id:
        return top.name
    return "Staff"


def star_display(rating: int) -> str:
    return "\u2605" * rating + "\u2606" * (5 - rating)


def record_staff_vouch(staff_id: str, verdict: str):
    data = load_staff_vouches()
    if staff_id not in data:
        data[staff_id] = {"total": 0, "positive": 0, "neutral": 0, "negative": 0}
    data[staff_id]["total"] += 1
    data[staff_id][verdict] += 1
    save_staff_vouches(data)
    return data[staff_id]


def record_product_vouch(product: str, stars: int):
    data = load_product_vouches()
    if product not in data:
        data[product] = {"count": 0, "total_stars": 0}
    data[product]["count"] += 1
    data[product]["total_stars"] += stars
    save_product_vouches(data)
    return data[product]


def build_vouch_embed(interaction: discord.Interaction, flow: "VouchFlow", comment: str) -> discord.Embed:
    staff = flow.staff
    voter = flow.target
    guild = interaction.guild

    stats = record_staff_vouch(str(staff.id), flow.verdict)

    embed = discord.Embed(
        title="New Staff Vouch",
        description="Customer feedback from a ticket.",
        color=COLOR[flow.verdict],
        timestamp=discord.utils.utcnow(),
    )
    embed.set_author(name=staff.display_name, icon_url=staff.display_avatar.url)
    embed.set_thumbnail(url=staff.display_avatar.url)
    embed.add_field(name="From", value=voter.mention, inline=True)
    embed.add_field(name="Staff", value=f"{staff.mention} | {staff_role_name(staff)}", inline=True)
    embed.add_field(name="Feedback", value=comment or "_No comment left._", inline=False)
    embed.set_footer(text=f"Staff total vouch count: {stats['total']}")
    return embed


def build_product_vouch_embed(
    user: discord.Member, product: str, stars: int, comment: str
) -> discord.Embed:
    stats = record_product_vouch(product, stars)
    avg = stats["total_stars"] / stats["count"]

    embed = discord.Embed(
        title="Product Vouch",
        description=f"{star_display(stars)}\n**{product}**",
        color=STAR_COLOR.get(stars, 0x5865F2),
        timestamp=discord.utils.utcnow(),
    )
    embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)
    embed.set_thumbnail(url=user.display_avatar.url)
    if comment:
        embed.add_field(name="Review", value=comment, inline=False)
    embed.set_footer(text=f"Vouched by {user.display_name} | Avg: {avg:.1f}★ ({stats['count']} reviews)")
    return embed


STAR_COLOR = {
    5: 0x5865F2,
    4: 0x57F287,
    3: 0xFEE75C,
    2: 0xEB459E,
    1: 0xED4245,
}


class VouchModal(discord.ui.Modal, title="Submit your vouch"):
    comment = discord.ui.TextInput(
        label="Comment / reason (optional)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=300,
        placeholder="How was the service? Anything to add?",
    )

    def __init__(self, flow: "VouchFlow"):
        super().__init__()
        self.flow = flow

    async def on_submit(self, interaction: discord.Interaction):
        flow = self.flow
        if flow.submitted:
            await interaction.response.send_message(
                "You've already submitted a vouch for this request.", ephemeral=True
            )
            return
        flow.submitted = True

        comment = self.comment.value.strip()
        embed = build_vouch_embed(interaction, flow, comment)
        try:
            webhook = discord.Webhook.from_url(WEBHOOK_URL, client=interaction.client)
            await webhook.send(
                embed=embed,
                username=flow.staff.display_name,
                avatar_url=flow.staff.display_avatar.url,
            )
        except discord.HTTPException as e:
            logging.error("Failed to post vouch to webhook: %s", e)
            flow.submitted = False
            await interaction.response.send_message(
                "Vouch could not be sent right now. Please try again.", ephemeral=True
            )
            return

        await flow.disable()
        await interaction.response.send_message(
            f"Your **{LABEL[flow.verdict]}** vouch for {flow.staff.mention} has been recorded.",
            ephemeral=True,
        )
        if flow.prompt_message is not None:
            try:
                await flow.prompt_message.channel.send(
                    f"{flow.target.mention} has vouched for {flow.staff.mention} and gave a **{LABEL[flow.verdict]}** review!"
                )
            except discord.HTTPException:
                pass


class VouchFlow(discord.ui.View):
    def __init__(self, target: discord.Member, staff: discord.Member):
        super().__init__(timeout=900)
        self.target = target
        self.staff = staff
        self.prompt_message: discord.Message | None = None
        self.verdict: str | None = None
        self.submitted = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.target.id:
            await interaction.response.send_message(
                f"This vouch request is for **{self.target.display_name}**, not you.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self):
        await self.disable()

    async def disable(self):
        for child in self.children:
            child.disabled = True
        if self.prompt_message is not None:
            try:
                await self.prompt_message.edit(view=self)
            except discord.HTTPException:
                pass

    async def _open_modal(self, interaction: discord.Interaction, verdict: str):
        if self.submitted:
            await interaction.response.send_message(
                "You've already submitted a vouch for this request.", ephemeral=True
            )
            return
        self.verdict = verdict
        await interaction.response.send_modal(VouchModal(self))

    @discord.ui.button(label="Positive", style=discord.ButtonStyle.success, emoji="\u2705")
    async def positive(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open_modal(interaction, "positive")

    @discord.ui.button(label="Neutral", style=discord.ButtonStyle.secondary, emoji="\U0001F610")
    async def neutral(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open_modal(interaction, "neutral")

    @discord.ui.button(label="Negative", style=discord.ButtonStyle.danger, emoji="\u274C")
    async def negative(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open_modal(interaction, "negative")


class ProductVouchModal(discord.ui.Modal, title="Anything else you'd like to say?"):
    comment = discord.ui.TextInput(
        label="Additional comments (optional)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=300,
        placeholder="Tell us more about your experience...",
    )

    def __init__(self, user: discord.Member, product: str, stars: int):
        super().__init__()
        self.user = user
        self.product = product
        self.stars = stars

    async def on_submit(self, interaction: discord.Interaction):
        comment = self.comment.value.strip()
        embed = build_product_vouch_embed(self.user, self.product, self.stars, comment)
        try:
            webhook = discord.Webhook.from_url(WEBHOOK_URL, client=interaction.client)
            await webhook.send(
                embed=embed,
                username=self.user.display_name,
                avatar_url=self.user.display_avatar.url,
            )
        except discord.HTTPException as e:
            logging.error("Failed to post product vouch to webhook: %s", e)
            await interaction.response.send_message(
                "Vouch could not be sent right now. Please try again.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"Your **{star_display(self.stars)}** vouch for **{self.product}** has been recorded!",
            ephemeral=True,
        )


@bot.tree.command(name="staffvouch", description="Ask a member to vouch for a staff member")
@app_commands.describe(target="The member being vouched for")
@app_commands.guild_only()
async def staffvouch(interaction: discord.Interaction, target: discord.Member):
    if not is_staff(interaction.user):
        await interaction.response.send_message(
            "You don't have permission to use this command.", ephemeral=True
        )
        return
    if "ticket" not in interaction.channel.name.lower():
        await interaction.response.send_message(
            "This command can only be used in ticket channels.", ephemeral=True
        )
        return
    if target.bot:
        await interaction.response.send_message("You can't vouch for a bot.", ephemeral=True)
        return
    if target.id == interaction.user.id:
        await interaction.response.send_message("You can't vouch for yourself.", ephemeral=True)
        return

    flow = VouchFlow(target=target, staff=interaction.user)

    embed = discord.Embed(
        title="Vouch Request",
        description=(
            f"**{target.display_name}**, {interaction.user.mention} has asked you to vouch for them.\n\n"
            "Rate your experience using the buttons below."
        ),
        color=0x5865F2,
    )
    embed.set_author(name=interaction.guild.name, icon_url=interaction.guild.icon.url if interaction.guild.icon else None)
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="Staff", value=interaction.user.mention, inline=True)
    embed.add_field(name="Requested", value=f"<t:{int(discord.utils.utcnow().timestamp())}>", inline=True)
    embed.set_footer(text="Only the requested member can respond · expires in 15 min")

    await interaction.response.send_message(embed=embed, view=flow)
    flow.prompt_message = await interaction.original_response()


@bot.tree.command(name="vouch", description="Leave a vouch for a product or service")
@app_commands.describe(product="What product/service are you vouching for?", stars="Star rating (1-5)")
@app_commands.choices(
    stars=[
        app_commands.Choice(name="\u2605 - Poor", value=1),
        app_commands.Choice(name="\u2605\u2605 - Fair", value=2),
        app_commands.Choice(name="\u2605\u2605\u2605 - Good", value=3),
        app_commands.Choice(name="\u2605\u2605\u2605\u2605 - Great", value=4),
        app_commands.Choice(name="\u2605\u2605\u2605\u2605\u2605 - Excellent", value=5),
    ]
)
@app_commands.guild_only()
async def vouch(interaction: discord.Interaction, product: str, stars: int):
    config = load_config()
    allowed_channel = config.get("vouch_channel_id")
    if allowed_channel and interaction.channel.id != allowed_channel:
        await interaction.response.send_message(
            "This command can only be used in the vouches channel.", ephemeral=True
        )
        return
    await interaction.response.send_modal(
        ProductVouchModal(interaction.user, product, stars)
    )


@bot.tree.command(name="vouches", description="View a staff member's vouch stats")
@app_commands.describe(member="The staff member to check")
@app_commands.guild_only()
async def vouches(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    data = load_staff_vouches()
    stats = data.get(str(target.id), {"total": 0, "positive": 0, "neutral": 0, "negative": 0})

    embed = discord.Embed(
        title=f"Vouch Stats — {target.display_name}",
        color=target.display_avatar.color or 0x5865F2,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="Total", value=str(stats["total"]), inline=True)
    embed.add_field(name="\u2705 Positive", value=str(stats["positive"]), inline=True)
    embed.add_field(name="\U0001F610 Neutral", value=str(stats["neutral"]), inline=True)
    embed.add_field(name="\u274C Negative", value=str(stats["negative"]), inline=True)

    if stats["total"] > 0:
        pos_pct = (stats["positive"] / stats["total"]) * 100
        embed.add_field(name="Positive Rate", value=f"{pos_pct:.0f}%", inline=True)

    embed.set_footer(text=f"Requested by {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="leaderboard", description="Top staff by vouch count")
@app_commands.guild_only()
async def leaderboard(interaction: discord.Interaction):
    data = load_staff_vouches()
    if not data:
        await interaction.response.send_message("No vouches recorded yet.", ephemeral=True)
        return

    sorted_staff = sorted(data.items(), key=lambda x: x[1]["total"], reverse=True)[:10]

    lines = []
    medals = ["\U0001F947", "\U0001F948", "\U0001F949"]
    for i, (staff_id, stats) in enumerate(sorted_staff):
        prefix = medals[i] if i < 3 else f"**#{i+1}**"
        lines.append(f"{prefix} <@{staff_id}> — **{stats['total']}** vouches (\u2705 {stats['positive']} | \U0001F610 {stats['neutral']} | \u274C {stats['negative']})")

    embed = discord.Embed(
        title="Staff Vouch Leaderboard",
        description="\n".join(lines),
        color=0x5865F2,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_footer(text=f"Requested by {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="removevouch", description="Remove a vouch from a staff member")
@app_commands.describe(member="The staff member to remove a vouch from", verdict="Which vouch type to remove")
@app_commands.choices(
    verdict=[
        app_commands.Choice(name="\u2705 Positive", value="positive"),
        app_commands.Choice(name="\U0001F610 Neutral", value="neutral"),
        app_commands.Choice(name="\u274C Negative", value="negative"),
    ]
)
@app_commands.guild_only()
async def removevouch(interaction: discord.Interaction, member: discord.Member, verdict: str):
    if not is_admin(interaction.user):
        await interaction.response.send_message(
            "You need the Manage Server permission to use this.", ephemeral=True
        )
        return

    data = load_staff_vouches()
    staff_id = str(member.id)
    if staff_id not in data or data[staff_id][verdict] <= 0:
        await interaction.response.send_message(
            f"No {LABEL[verdict]} vouches to remove for {member.mention}.", ephemeral=True
        )
        return

    data[staff_id]["total"] -= 1
    data[staff_id][verdict] -= 1
    save_staff_vouches(data)

    await interaction.response.send_message(
        f"Removed one **{LABEL[verdict]}** vouch from {member.mention}. New total: **{data[staff_id]['total']}**."
    )


@bot.tree.command(name="vouchstats", description="View stats for a product")
@app_commands.describe(product="The product name to check")
@app_commands.guild_only()
async def vouchstats(interaction: discord.Interaction, product: str):
    data = load_product_vouches()
    if product not in data:
        await interaction.response.send_message(
            f"No vouches found for **{product}**.", ephemeral=True
        )
        return

    stats = data[product]
    avg = stats["total_stars"] / stats["count"]

    embed = discord.Embed(
        title=f"Product Stats — {product}",
        color=STAR_COLOR.get(round(avg), 0x5865F2),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Total Vouches", value=str(stats["count"]), inline=True)
    embed.add_field(name="Average Stars", value=f"{avg:.1f} {star_display(round(avg))}", inline=True)
    embed.add_field(name="Total Stars", value=str(stats["total_stars"]), inline=True)
    embed.set_footer(text=f"Requested by {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="setvouchchannel", description="Set the channel for /vouch")
@app_commands.describe(channel="The channel to allow /vouch in")
@app_commands.guild_only()
async def setvouchchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not is_admin(interaction.user):
        await interaction.response.send_message(
            "You need the Manage Server permission to use this.", ephemeral=True
        )
        return

    config = load_config()
    config["vouch_channel_id"] = channel.id
    save_config(config)

    await interaction.response.send_message(
        f"/vouch is now restricted to {channel.mention}."
    )


live_leaderboard_task: asyncio.Task | None = None


async def fetch_coupon_leaderboard_data():
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"https://api.sellauth.com/v1/shops/{SELLAUTH_SHOP_ID}/coupons",
            headers={"Authorization": f"Bearer {SELLAUTH_API_KEY}"},
        ) as resp:
            if resp.status != 200:
                return None
            return await resp.json()


def build_leaderboard_embed(coupons_data, seconds_remaining=None):
    coupon_list = coupons_data.get("data", [])
    sorted_coupons = sorted(coupon_list, key=lambda c: c.get("uses", 0), reverse=True)[:10]

    medals = ["\U0001F947", "\U0001F948", "\U0001F949"]
    lines = []
    for i, c in enumerate(sorted_coupons):
        prefix = medals[i] if i < 3 else f"**#{i+1}**"
        code = c.get("code", "???")
        uses = c.get("uses", 0)
        discount = c.get("discount", "0")
        ctype = c.get("type", "percentage")
        if ctype == "percentage":
            discount_str = f"{discount}%"
        else:
            discount_str = f"${discount}"
        lines.append(f"{prefix} **{code}** — {uses} uses ({discount_str} off)")

    embed = discord.Embed(
        title="Coupon Leaderboard",
        description="\n".join(lines),
        color=0x5865F2,
        timestamp=discord.utils.utcnow(),
    )
    if seconds_remaining is not None:
        embed.set_footer(text=f"Leaderboard will refresh in {seconds_remaining} seconds!")
    else:
        embed.set_footer(text="From onnyxtweaks.mysellauth.com")
    return embed


async def live_leaderboard_loop(message: discord.Message):
    global live_leaderboard_task
    try:
        while True:
            await asyncio.sleep(30)
            try:
                data = await fetch_coupon_leaderboard_data()
                if data is None:
                    logging.warning("Live leaderboard: API returned None")
                    continue
                embed = build_leaderboard_embed(data, seconds_remaining=30)
                await message.edit(embed=embed)
            except discord.HTTPException as e:
                logging.error("Live leaderboard edit failed: %s", e)
                break
            except Exception as e:
                logging.error("Live leaderboard error: %s", e)
                continue
    except asyncio.CancelledError:
        logging.info("Live leaderboard cancelled")
    finally:
        live_leaderboard_task = None


@bot.tree.command(name="leaderboardwebsite", description="Most used coupon codes from the website")
@app_commands.guild_only()
async def leaderboardwebsite(interaction: discord.Interaction):
    global live_leaderboard_task
    if not SELLAUTH_SHOP_ID or not SELLAUTH_API_KEY:
        await interaction.response.send_message(
            "SellAuth API is not configured.", ephemeral=True
        )
        return

    await interaction.response.defer()

    data = await fetch_coupon_leaderboard_data()
    if data is None:
        await interaction.followup.send("Failed to fetch coupons.", ephemeral=True)
        return

    coupon_list = data.get("data", [])
    if not coupon_list:
        await interaction.followup.send("No coupons found.", ephemeral=True)
        return

    embed = build_leaderboard_embed(data, seconds_remaining=30)
    msg = await interaction.followup.send(embed=embed)

    if live_leaderboard_task is not None:
        live_leaderboard_task.cancel()
    live_leaderboard_task = asyncio.create_task(live_leaderboard_loop(msg))


COUPON_CREATOR_ROLE_ID = 1546664972774412318


def has_coupon_creator_role(member: discord.Member) -> bool:
    return any(r.id == COUPON_CREATOR_ROLE_ID for r in member.roles)


@bot.tree.command(name="couponcreate", description="Create a coupon code on the website")
@app_commands.describe(
    code="Coupon code (e.g. SUMMER20)",
    discount="Discount percentage (e.g. 20 for 20% off)",
    max_uses="Maximum uses (leave empty for unlimited)",
)
@app_commands.guild_only()
async def couponcreate(
    interaction: discord.Interaction,
    code: str,
    discount: int,
    max_uses: int = None,
):
    if not has_coupon_creator_role(interaction.user):
        await interaction.response.send_message(
            "You need the Coupon Creator role to use this.", ephemeral=True
        )
        return
    if "ticket" not in interaction.channel.name.lower():
        await interaction.response.send_message(
            "This command can only be used in ticket channels.", ephemeral=True
        )
        return

    if not SELLAUTH_SHOP_ID or not SELLAUTH_API_KEY:
        await interaction.response.send_message(
            "SellAuth API is not configured.", ephemeral=True
        )
        return

    await interaction.response.defer()

    payload = {
        "code": code.upper(),
        "global": True,
        "discount": discount,
        "type": "percentage",
        "disable_if_volume_discount": False,
    }
    if max_uses is not None:
        payload["max_uses"] = max_uses

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"https://api.sellauth.com/v1/shops/{SELLAUTH_SHOP_ID}/coupons",
                headers={
                    "Authorization": f"Bearer {SELLAUTH_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
            ) as resp:
                result = await resp.json()
                if resp.status not in (200, 201):
                    msg = result.get("message", "Unknown error")
                    await interaction.followup.send(
                        f"Failed to create coupon: {msg}", ephemeral=True
                    )
                    return
    except Exception as e:
        logging.error("SellAuth API error: %s", e)
        await interaction.followup.send(
            "Error connecting to SellAuth API.", ephemeral=True
        )
        return

    discount_str = f"{discount}% off"
    uses_str = f"Max uses: {max_uses}" if max_uses else "Unlimited uses"

    embed = discord.Embed(
        title="Coupon Created",
        color=0x57F287,
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Code", value=f"`{code.upper()}`", inline=True)
    embed.add_field(name="Discount", value=discount_str, inline=True)
    embed.add_field(name="Uses", value=uses_str, inline=True)
    embed.set_footer(text=f"Created by {interaction.user.display_name}")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="couponinfo", description="Look up a coupon code and see its usage stats")
@app_commands.describe(code="The coupon code to look up")
@app_commands.guild_only()
async def couponinfo(interaction: discord.Interaction, code: str):
    if "ticket" not in interaction.channel.name.lower():
        await interaction.response.send_message(
            "This command can only be used in ticket channels.", ephemeral=True
        )
        return

    if not SELLAUTH_SHOP_ID or not SELLAUTH_API_KEY:
        await interaction.response.send_message(
            "SellAuth API is not configured.", ephemeral=True
        )
        return

    await interaction.response.defer()

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.sellauth.com/v1/shops/{SELLAUTH_SHOP_ID}/coupons",
                headers={"Authorization": f"Bearer {SELLAUTH_API_KEY}"},
            ) as resp:
                if resp.status != 200:
                    await interaction.followup.send(
                        f"Failed to fetch coupons (API returned {resp.status})."
                    )
                    return
                result = await resp.json()

        coupon_list = result.get("data", [])
        target = None
        for c in coupon_list:
            if c.get("code", "").upper() == code.upper():
                target = c
                break

        if not target:
            await interaction.followup.send(
                f"Coupon `{code.upper()}` not found."
            )
            return

        uses = target.get("uses", 0)
        discount = target.get("discount", "0")
        ctype = target.get("type", "percentage")
        max_uses = target.get("max_uses")

        if ctype == "percentage":
            discount_str = f"{discount}%"
        else:
            discount_str = f"${discount}"

        uses_str = f"{uses}" if max_uses is None else f"{uses} / {max_uses}"

        embed = discord.Embed(
            title=f"Coupon Info — {target.get('code', '???').upper()}",
            color=0x5865F2,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Discount", value=discount_str, inline=True)
        embed.add_field(name="Uses", value=uses_str, inline=True)
        embed.add_field(name="Type", value=ctype.title(), inline=True)
        embed.set_footer(text=f"Requested by {interaction.user.display_name}")
        await interaction.followup.send(embed=embed)

    except Exception as e:
        logging.error("SellAuth API error: %s", e)
        await interaction.followup.send(
            "Error connecting to SellAuth API."
        )


@bot.tree.command(name="couponremove", description="Remove a coupon code from the website")
@app_commands.describe(code="The coupon code to delete")
@app_commands.guild_only()
async def couponremove(interaction: discord.Interaction, code: str):
    if not has_coupon_creator_role(interaction.user):
        await interaction.response.send_message(
            "You need the Coupon Creator role to use this.", ephemeral=True
        )
        return
    if "ticket" not in interaction.channel.name.lower():
        await interaction.response.send_message(
            "This command can only be used in ticket channels.", ephemeral=True
        )
        return

    if not SELLAUTH_SHOP_ID or not SELLAUTH_API_KEY:
        await interaction.response.send_message(
            "SellAuth API is not configured.", ephemeral=True
        )
        return

    await interaction.response.defer()

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.sellauth.com/v1/shops/{SELLAUTH_SHOP_ID}/coupons",
                headers={"Authorization": f"Bearer {SELLAUTH_API_KEY}"},
            ) as resp:
                result = await resp.json()
                coupon_list = result.get("data", [])

            target = None
            for c in coupon_list:
                if c.get("code", "").upper() == code.upper():
                    target = c
                    break

            if not target:
                await interaction.followup.send(
                    f"Coupon `{code.upper()}` not found.", ephemeral=True
                )
                return

            coupon_id = target["id"]
            async with session.delete(
                f"https://api.sellauth.com/v1/shops/{SELLAUTH_SHOP_ID}/coupons/{coupon_id}",
                headers={"Authorization": f"Bearer {SELLAUTH_API_KEY}"},
            ) as resp:
                if resp.status not in (200, 204):
                    await interaction.followup.send(
                        f"Failed to delete coupon (API returned {resp.status}).", ephemeral=True
                    )
                    return
    except Exception as e:
        logging.error("SellAuth API error: %s", e)
        await interaction.followup.send(
            "Error connecting to SellAuth API.", ephemeral=True
        )
        return

    embed = discord.Embed(
        title="Coupon Deleted",
        description=f"**{code.upper()}** has been removed.",
        color=0xED4245,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_footer(text=f"Deleted by {interaction.user.display_name}")
    await interaction.followup.send(embed=embed)


@bot.event
async def on_ready():
    logging.info("Logged in as %s (%s)", bot.user, bot.user.id)
    try:
        synced = await bot.tree.sync()
        logging.info("Synced %s slash command(s)", len(synced))
    except Exception as e:
        logging.error("Failed to sync commands: %s", e)


def acquire_lock() -> socket.socket | None:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", LOCK_PORT))
        sock.listen(1)
        return sock
    except OSError:
        return None


if __name__ == "__main__":
    _lock = acquire_lock()
    if _lock is None:
        logging.warning("Another instance is already running, exiting.")
        raise SystemExit(0)
    bot.run(TOKEN)