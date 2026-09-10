import discord
from discord.ext import commands
import aiohttp
import asyncio
import pyotp
import os
import json
import time

BASE = "https://discord.com/api/v9"
BOT_TOKEN = os.getenv("DISCORD_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # Bildirim için
CHECK_INTERVAL = 1.0        # Her 1 saniyede bir kontrol
CLAIM_LOOP_DELAY = 0.5      # Boşa düşünce 0.5s'de bir tekrar dene
MAX_CLAIM_ATTEMPTS = 20     # 0.5s x 20 = 10 saniye boyunca dene

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

DATA_FILE = "accounts.json"
stop_flags = {}     # {user_id: bool}
active_tasks = {}   # {user_id: [task, task, ...]}


# ============ VERİ ============

def load_accounts():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_accounts(accounts):
    with open(DATA_FILE, "w") as f:
        json.dump(accounts, f, indent=2)

accounts = load_accounts()


# ============ WEBHOOK ============

async def send_webhook(content: str, embed: discord.Embed = None, color: int = 0x5865F2):
    """Webhook'a bildirim gönder."""
    if not WEBHOOK_URL:
        return
    payload = {"content": content}
    if embed:
        payload["embeds"] = [embed.to_dict()]
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(WEBHOOK_URL, json=payload, timeout=10)
    except Exception as e:
        print(f"Webhook hatası: {e}")


# ============ MODALLAR ============

class AccountModal(discord.ui.Modal, title="Hesap Ekle"):
    account_name = discord.ui.TextInput(
        label="Hesap Adı", placeholder="Örn: main_account",
        required=True, max_length=50
    )
    token = discord.ui.TextInput(
        label="Discord Token", placeholder="User token",
        required=True, style=discord.TextStyle.paragraph, max_length=200
    )
    password = discord.ui.TextInput(
        label="Discord Şifre", placeholder="Hesap şifresi",
        required=True, style=discord.TextStyle.short, max_length=200
    )
    totp_secret = discord.ui.TextInput(
        label="2FA TOTP Secret", placeholder="Discord 2FA secret key",
        required=True, style=discord.TextStyle.short, max_length=100
    )
    guild_id = discord.ui.TextInput(
        label="Hedef Guild ID", placeholder="Sunucu ID",
        required=True, style=discord.TextStyle.short, max_length=30
    )

    async def on_submit(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)
        if user_id not in accounts:
            accounts[user_id] = {}

        accounts[user_id][self.account_name.value] = {
            "token": self.token.value.strip(),
            "password": self.password.value.strip(),
            "totp_secret": self.totp_secret.value.strip(),
            "guild_id": self.guild_id.value.strip(),
            "vanities": []
        }
        save_accounts(accounts)

        await interaction.response.send_message(
            f"✅ **{self.account_name.value}** hesabı eklendi!\n"
            f"Guild ID: `{self.guild_id.value}`\n\n"
            f"Şimdi **🔗 Vanity Ekle** butonuna bas.",
            ephemeral=True
        )
        await send_webhook(
            f"➕ Yeni hesap eklendi: **{self.account_name.value}**\n"
            f"Guild: `{self.guild_id.value}`\n"
            f"Kullanıcı: {interaction.user}"
        )


class VanityAddModal(discord.ui.Modal, title="Vanity URL Ekle"):
    account_name = discord.ui.TextInput(
        label="Hesap Adı", placeholder="Hangi hesap için?",
        required=True, max_length=50
    )
    vanity_urls = discord.ui.TextInput(
        label="Vanity URL'ler (her satıra bir tane)",
        placeholder="cool\nepic\nrare-name",
        required=True, style=discord.TextStyle.paragraph, max_length=500
    )

    async def on_submit(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)
        acc = self.account_name.value.strip()

        if user_id not in accounts or acc not in accounts[user_id]:
            await interaction.response.send_message(
                f"❌ **{acc}** adında hesap bulunamadı. Önce `/panel` → **➕ Hesap Ekle**.",
                ephemeral=True
            )
            return

        vanities = [l.strip() for l in self.vanity_urls.value.splitlines() if l.strip()]
        if not vanities:
            await interaction.response.send_message("❌ En az bir URL gerekli!", ephemeral=True)
            return

        for v in vanities:
            if len(v) > 25:
                await interaction.response.send_message(
                    f"❌ Çok uzun: `{v}` (max 25)", ephemeral=True
                )
                return
            if not all(c.islower() or c.isdigit() or c == '-' for c in v):
                await interaction.response.send_message(
                    f"❌ Geçersiz karakter: `{v}`\nSadece küçük harf, sayı, - kullan.",
                    ephemeral=True
                )
                return

        accounts[user_id][acc]["vanities"] = vanities
        save_accounts(accounts)

        await interaction.response.send_message(
            f"✅ **{acc}** için {len(vanities)} vanity eklendi:\n"
            + "\n".join(f"• `{v}`" for v in vanities),
            ephemeral=True
        )


# ============ PANEL ============

class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="➕ Hesap Ekle", style=discord.ButtonStyle.green, custom_id="add_account")
    async def add_account(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AccountModal())

    @discord.ui.button(label="🔗 Vanity Ekle", style=discord.ButtonStyle.blurple, custom_id="add_vanity")
    async def add_vanity(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(VanityAddModal())

    @discord.ui.button(label="📋 Hesapları Listele", style=discord.ButtonStyle.secondary, custom_id="list_accounts")
    async def list_accounts(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = str(interaction.user.id)
        if user_id not in accounts or not accounts[user_id]:
            await interaction.response.send_message("❌ Henüz hesap eklenmemiş.", ephemeral=True)
            return

        embed = discord.Embed(title="📋 Hesapların", color=discord.Color.gold())
        for name, data in accounts[user_id].items():
            vanities = data.get("vanities", [])
            embed.add_field(
                name=f"👤 {name}",
                value=f"Guild: `{data['guild_id']}`\n"
                      f"Vanities ({len(vanities)}): "
                      + (", ".join(f"`{v}`" for v in vanities[:5]) if vanities else "_Yok_"),
                inline=False
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="▶ İzlemeye Başla", style=discord.ButtonStyle.danger, custom_id="start_snipe")
    async def start_snipe(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = str(interaction.user.id)
        if user_id not in accounts or not accounts[user_id]:
            await interaction.response.send_message("❌ Önce hesap ekle!", ephemeral=True)
            return

        stop_flags[user_id] = False
        started = []

        for acc_name, acc_data in accounts[user_id].items():
            if not acc_data.get("vanities"):
                continue

            task = asyncio.create_task(
                run_sniper(
                    interaction.user,
                    acc_name,
                    acc_data,
                    interaction.channel,
                    user_id
                )
            )
            active_tasks.setdefault(user_id, []).append(task)
            started.append(acc_name)

        if not started:
            await interaction.response.send_message(
                "❌ Hiçbir hesapta vanity URL yok. **🔗 Vanity Ekle** ile ekle.",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"🚀 **{len(started)} hesap** için izleme başlatıldı:\n"
            + "\n".join(f"• `{a}`" for a in started)
            + f"\n\nKontrol aralığı: **{CHECK_INTERVAL}s**\n"
            f"Boşa düşünce: **{CLAIM_LOOP_DELAY}s**'de bir tekrar deneme",
            ephemeral=True
        )
        await send_webhook(
            f"🚀 İzleme başlatıldı\n"
            f"Hesaplar: {', '.join(started)}\n"
            f"Kullanıcı: {interaction.user}"
        )

    @discord.ui.button(label="⏹ Durdur", style=discord.ButtonStyle.secondary, custom_id="stop_snipe")
    async def stop_snipe(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = str(interaction.user.id)
        stop_flags[user_id] = True

        # Aktif task'leri iptal et
        for t in active_tasks.get(user_id, []):
            if not t.done():
                t.cancel()
        active_tasks[user_id] = []

        await interaction.response.send_message("⏹ İzleme durduruldu.", ephemeral=True)
        await send_webhook(f"⏹ İzleme durduruldu. Kullanıcı: {interaction.user}")


# ============ SNIPER MANTIĞI ============

async def run_sniper(user, account_name, account_data, channel, user_id):
    """Bir hesap için vanity URL'leri izle."""
    token = account_data["token"]
    totp_secret = account_data["totp_secret"]
    guild_id = account_data["guild_id"]
    vanities = account_data["vanities"]

    headers = {
        "Authorization": token,
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Origin": "https://discord.com",
        "Referer": "https://discord.com/channels/@me"
    }

    connector = aiohttp.TCPConnector(limit=50, ttl_dns_cache=300)
    timeout = aiohttp.ClientTimeout(total=10)

    async with aiohttp.ClientSession(headers=headers, connector=connector, timeout=timeout) as session:
        # Token doğrulama
        try:
            async with session.get(f"{BASE}/users/@me") as r:
                if r.status != 200:
                    msg = f"❌ **{account_name}**: Token geçersiz ({r.status})"
                    await channel.send(msg)
                    await send_webhook(msg)
                    return
                me = await r.json()
                await channel.send(f"✅ **{account_name}**: `{me['username']}` giriş yapıldı")
        except Exception as e:
            msg = f"❌ **{account_name}**: Bağlantı hatası - {e}"
            await channel.send(msg)
            await send_webhook(msg)
            return

        # Mevcut vanity kontrolü
        current_vanity = await get_guild_vanity(session, guild_id)
        if current_vanity:
            msg = (f"⚠️ **{account_name}**: Sunucuda zaten vanity var: `{current_vanity}`\n"
                   f"Boşalana kadar beklenecek, mevcut vanity ezilmeyecek.")
            await channel.send(msg)
            await send_webhook(msg)
        else:
            await channel.send(f"✅ **{account_name}**: Sunucuda vanity yok, izleme başlıyor...")

        check_count = {v: 0 for v in vanities}

        while not stop_flags.get(user_id, False):
            for vanity in vanities:
                if stop_flags.get(user_id, False):
                    break

                check_count[vanity] += 1

                # 1. kontrol
                available = await check_vanity(session, vanity)

                if available is True:
                    # 2. kontrol (yanlış pozitif olmasın)
                    recheck = await check_vanity(session, vanity)
                    if recheck is not True:
                        continue

                    msg = f"🎉 **{account_name}**: `{vanity}` BOŞTA! Anında alınıyor..."
                    await channel.send(msg)
                    await send_webhook(msg)

                    # Mevcut vanity var mı? Varsa çekme
                    current = await get_guild_vanity(session, guild_id)
                    if current:
                        msg = f"⛔ **{account_name}**: Sunucuda zaten vanity var (`{current}`), atlanıyor."
                        await channel.send(msg)
                        await send_webhook(msg)
                        continue

                    # 0.5s aralıklarla hızlı alma denemesi
                    success = False
                    for attempt in range(1, MAX_CLAIM_ATTEMPTS + 1):
                        if stop_flags.get(user_id, False):
                            break

                        success = await claim_vanity(session, guild_id, vanity, totp_secret)
                        if success:
                            break

                        await asyncio.sleep(CLAIM_LOOP_DELAY)

                    if success:
                        msg = f"✅ **{account_name}**: `{vanity}` BAŞARIYLA ALINDI! 🎉"
                        await channel.send(msg)
                        await send_webhook(
                            msg,
                            embed=discord.Embed(
                                title="🎯 Vanity Alındı!",
                                description=f"Hesap: **{account_name}**\n"
                                            f"Vanity: `{vanity}`\n"
                                            f"Guild: `{guild_id}`\n"
                                            f"Zaman: <t:{int(time.time())}:F>",
                                color=0x00FF00
                            )
                        )
                        stop_flags[user_id] = True
                        break
                    else:
                        msg = f"❌ **{account_name}**: `{vanity}` {MAX_CLAIM_ATTEMPTS} denemede alınamadı."
                        await channel.send(msg)
                        await send_webhook(msg)

                elif available is None:
                    if check_count[vanity] % 50 == 0:
                        await channel.send(f"⚠️ **{account_name}**: `{vanity}` kontrol hatası (devam)")

                else:
                    if check_count[vanity] % 60 == 0:  # ~1 dakikada bir log
                        await channel.send(
                            f"🔍 **{account_name}**: `{vanity}` hala dolu. "
                            f"({check_count[vanity]} kontrol)"
                        )

            # Her saniye başına kontrol
            await asyncio.sleep(CHECK_INTERVAL)

        await channel.send(f"⏹ **{account_name}** izleme bitti.")


# ============ YARDIMCI FONKSİYONLAR ============

async def get_guild_vanity(session, guild_id):
    try:
        async with session.get(f"{BASE}/guilds/{guild_id}/vanity-url") as r:
            if r.status == 200:
                data = await r.json()
                return data.get("code")
            return None
    except Exception:
        return None


async def check_vanity(session, vanity):
    try:
        async with session.get(f"{BASE}/invites/{vanity}") as r:
            if r.status == 404:
                return True
            if r.status == 200:
                return False
            if r.status == 429:
                data = await r.json()
                retry = data.get("retry_after", 1)
                await asyncio.sleep(retry + 0.1)
                return await check_vanity(session, vanity)
            return None
    except Exception:
        return None


async def claim_vanity(session, guild_id, vanity, totp_secret):
    payload = {"code": vanity}

    try:
        async with session.patch(f"{BASE}/guilds/{guild_id}/vanity-url", json=payload) as r:
            if r.status == 200:
                return True

            if r.status == 429:
                data = await r.json()
                retry = data.get("retry_after", 1)
                await asyncio.sleep(retry + 0.1)
                return await claim_vanity(session, guild_id, vanity, totp_secret)

            if r.status == 403:
                text = await r.text()
                if "mfa" in text.lower() or "2fa" in text.lower():
                    # 2FA ile dene
                    try:
                        totp = pyotp.TOTP(totp_secret)
                        mfa_code = totp.now()
                    except Exception:
                        return False

                    mfa_headers = {
                        "X-Discord-MFA-Authorization": mfa_code,
                        "Content-Type": "application/json"
                    }
                    async with session.patch(
                        f"{BASE}/guilds/{guild_id}/vanity-url",
                        json=payload, headers=mfa_headers
                    ) as r2:
                        return r2.status == 200
                return False

            return False
    except Exception:
        return False


# ============ SLASH COMMAND ============

@bot.event
async def on_ready():
    print(f"✅ Bot hazır: {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"✅ {len(synced)} slash command senkronize edildi.")
    except Exception as e:
        print(f"❌ Sync hatası: {e}")


@bot.tree.command(name="panel", description="Vanity Sniper panelini aç")
async def panel(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🎯 Vanity URL Sniper Panel",
        description=(
            "**Kullanım:**\n"
            "1️⃣ **➕ Hesap Ekle** — token, şifre, 2FA, guild ID\n"
            "2️⃣ **🔗 Vanity Ekle** — her satıra bir URL\n"
            "3️⃣ **▶ İzlemeye Başla** — tüm hesaplar paralel çalışır\n\n"
            f"⏱️ Kontrol: **{CHECK_INTERVAL}s**\n"
            f"⚡ Alma denemesi: **{CLAIM_LOOP_DELAY}s** aralıkla\n"
            f"🔔 Webhook: {'✅ Aktif' if WEBHOOK_URL else '❌ Kapalı'}"
        ),
        color=discord.Color.blue()
    )
    await interaction.response.send_message(embed=embed, view=PanelView(), ephemeral=True)


# ============ BAŞLAT ============

if __name__ == "__main__":
    if not BOT_TOKEN:
        print("❌ DISCORD_TOKEN environment variable gerekli!")
        exit(1)
    if not WEBHOOK_URL:
        print("⚠️  WEBHOOK_URL tanımlı değil — webhook bildirimi kapalı.")
    bot.run(BOT_TOKEN)
