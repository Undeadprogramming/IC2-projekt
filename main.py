# main.py
import sys, asyncio, os, argparse, json, csv, re, logging, warnings
from datetime import datetime, timezone
from dotenv import load_dotenv
import discord

# hide Windows asyncio deprecation warnings (optional)
warnings.filterwarnings("ignore", message=".*WindowsSelectorEventLoopPolicy.*", category=DeprecationWarning)
warnings.filterwarnings("ignore", message=".*set_event_loop_policy.*", category=DeprecationWarning)

# Windows event loop policy fix (safe)
policy = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
if sys.platform.startswith("win") and policy:
    try:
        asyncio.set_event_loop_policy(policy())
    except Exception:
        pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("extractor")

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

MENTION_RE = re.compile(r"^<@!?(\d+)>$")


def tokens(s: str):
    return [x.strip() for x in re.split(r"[,\s]+", s or "") if x.strip()]


def norm_user(t: str) -> str:
    t = (t or "").strip()
    m = MENTION_RE.match(t)
    return m.group(1) if m else t


def save_json(items, out_path):
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    logger.info(f"Uloženo JSON: {out_path}")


def save_csv(items, out_path):
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    if not items:
        logger.warning("Žádná data k uložení do CSV.")
        return
    keys = sorted({k for it in items for k in it.keys()})
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for it in items:
            w.writerow({k: it.get(k, "") for k in keys})
    logger.info(f"Uloženo CSV: {out_path}")


def utc_iso(dt):
    if not dt:
        return ""
    return (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)).isoformat()


def message_to_record(msg: discord.Message):
    return {
        "id": str(msg.id),
        "channel_id": str(getattr(msg.channel, "id", "")),
        "channel_name": getattr(msg.channel, "name", ""),
        "guild_id": str(msg.guild.id) if msg.guild else "",
        "guild_name": msg.guild.name if msg.guild else "",
        "author_id": str(getattr(msg.author, "id", "")),
        "author_name": str(msg.author),
        "timestamp": utc_iso(msg.created_at),
        "content": msg.content,
        "attachments": [a.url for a in msg.attachments],
        "embeds": [str(e.to_dict()) for e in msg.embeds],
        "pinned": msg.pinned,
        "edited_timestamp": utc_iso(msg.edited_at),
    }


class ExtractorBot(discord.Client):
    def __init__(self, out_dir, mode, channels, users, pick_channels, pick_users,
                 history_limit, scan_limit, guild_id, **kwargs):
        super().__init__(**kwargs)
        self.out_dir = out_dir
        self.mode = mode  # passive | active | both

        self.channel_filter = set(map(str, channels)) if channels else None
        self.pick_channels = bool(pick_channels)

        self.user_tokens = [norm_user(x) for x in (users or [])]
        self.pick_users = bool(pick_users)
        self.user_filter_ids = None  # set[str] nebo None

        self.history_limit = int(history_limit or 10)
        self.scan_limit = int(scan_limit if scan_limit is not None else self.history_limit)
        self.target_guild = str(guild_id) if guild_id else None

        logger.info(
            f"Bot inicializován. mode={self.mode}, out={out_dir}, "
            f"pick_channels={self.pick_channels}, pick_users={self.pick_users}, "
            f"channels={len(self.channel_filter) if self.channel_filter else 0}, users={len(self.user_tokens)}, "
            f"history_limit={self.history_limit}, scan_limit={self.scan_limit}"
        )

    def iter_guilds(self):
        if not self.target_guild:
            return list(self.guilds)
        return [g for g in self.guilds if str(g.id) == self.target_guild]

    def channel_allowed(self, ch) -> bool:
        if not self.channel_filter:
            return True
        return str(getattr(ch, "id", "")) in self.channel_filter or getattr(ch, "name", "") in self.channel_filter

    def author_allowed(self, author) -> bool:
        if not self.user_filter_ids:
            return True
        return str(getattr(author, "id", "")) in self.user_filter_ids

    async def ainput(self, prompt: str) -> str:
        try:
            return await asyncio.to_thread(input, prompt)
        except Exception:
            return ""

    async def setup_channel_filter(self):
        if not self.pick_channels:
            return
        if self.channel_filter:
            raise ValueError("Nepoužívej zároveň --pick-channels a --channels.")

        guilds = self.iter_guilds()
        if not guilds:
            raise ValueError("Nemám žádný server (guild) pro výběr kanálů.")
        g = guilds[0]
        chans = list(getattr(g, "text_channels", []) or [])
        if not chans:
            raise ValueError("Na serveru nejsou textové kanály.")

        logger.info("=== VÝBĚR KANÁLŮ ===")
        for i, ch in enumerate(chans, 1):
            logger.info(f"{i:>3}. #{ch.name} ({ch.id})")

        line = await self.ainput("Kanály (ID nebo čísla)> ")

        picked = set()
        for t in tokens(line):
            if t.isdigit() and 1 <= int(t) <= len(chans):
                picked.add(str(chans[int(t) - 1].id))
            else:
                picked.add(t)

        if not picked:
            raise ValueError("Nevybral jsi žádný kanál.")

        self.channel_filter = picked
        logger.info(f"Vybrané kanály: {sorted(self.channel_filter)}")

    async def collect_authors_from_history(self):
        counts, names = {}, {}
        guilds = self.iter_guilds()
        if not guilds:
            return []

        for g in guilds:
            for ch in getattr(g, "text_channels", []):
                if not self.channel_allowed(ch):
                    continue
                try:
                    async for msg in ch.history(limit=self.scan_limit):
                        aid = str(getattr(msg.author, "id", ""))
                        if not aid:
                            continue
                        counts[aid] = counts.get(aid, 0) + 1
                        names.setdefault(aid, str(msg.author))
                except discord.Forbidden:
                    logger.error(f"Chybí oprávnění pro #{ch.name} ({ch.id}).")
                except Exception as e:
                    logger.error(f"Chyba při čtení #{ch.name}: {e}")

        authors = [{"id": aid, "name": names.get(aid, aid), "count": c} for aid, c in counts.items()]
        authors.sort(key=lambda x: (-x["count"], x["name"].lower()))
        return authors

    async def setup_user_filter(self):
        # neinteraktivně přes --users (jen ID/mention)
        if self.user_tokens and not self.pick_users:
            ids = []
            for t in self.user_tokens:
                t = norm_user(t)
                if not t.isdigit():
                    raise ValueError(f"--users: povoleno jen ID/mention. Neplatné: {t}")
                ids.append(t)
            self.user_filter_ids = set(ids)
            logger.info(f"User filtr: {sorted(self.user_filter_ids)}")
            return

        if not self.pick_users:
            return

        authors = await self.collect_authors_from_history()
        if not authors:
            raise ValueError("Nenašel jsem žádné autory (zvyš scan-limit nebo vyber jiné kanály).")

        logger.info(f"Nalezeno {len(authors)} unikátních autorů (scan-limit={self.scan_limit}).")
        logger.info("Dynamický výběr: vyber libovolně a napiš 'done' pro dokončení.")

        logger.info("=== UŽIVATELÉ ZE ZPRÁV ===")
        for i, a in enumerate(authors[:200], 1):
            logger.info(f"{i:>4}. {a['name']} <{a['id']}> msgs={a['count']}")
        logger.info("Zadej čísla nebo user ID (rozděl mezerou/čárkou); příkazy: done | all | clear")

        selected = set()
        while True:
            line = (await self.ainput(f"Users (ID nebo čísla)> ")).strip()
            if not line:
                continue

            low = line.lower()
            if low == "all":
                selected = {a["id"] for a in authors}
                break
            if low == "clear":
                selected.clear()
                logger.info("Výběr vymazán.")
                continue
            if low == "done":
                if selected:
                    break
                logger.info("Vyber alespoň 1 uživatele (nebo použij all).")
                continue

            for t in map(norm_user, tokens(line)):
                if t.isdigit() and len(t) >= 17:
                    selected.add(t)  # user ID
                elif t.isdigit() and 1 <= int(t) <= len(authors):
                    selected.add(authors[int(t) - 1]["id"])  # index

            logger.info(f"Zatím vybráno: {len(selected)}")

        self.user_filter_ids = selected
        logger.info(f"User filtr: {sorted(self.user_filter_ids)}")

    async def on_ready(self):
        logger.info(f"Přihlášen jako {self.user} (id: {self.user.id})")

        logger.info("============== DISCORD PŘEHLED ==============")
        for g in self.iter_guilds():
            logger.info(f"Server: {g.name} ({g.id})")
            for ch in getattr(g, "text_channels", []):
                logger.info(f" ├─ #{ch.name} ({ch.id})")
        logger.info("=============================================")

        try:
            await self.setup_channel_filter()
            await self.setup_user_filter()
        except Exception as e:
            logger.error(f"Chyba při nastavení filtrů: {e}")
            return await self.close()

        if self.mode in ("active", "both"):
            await self.bulk_export()
            if self.mode == "active":
                return await self.close()

        logger.info("Pasivní režim: čekám na nové zprávy... (ukonči Ctrl+C)")

    async def bulk_export(self):
        all_records = []
        for g in self.iter_guilds():
            logger.info(f"Zpracovávám server: {g.name} ({g.id})")
            for ch in getattr(g, "text_channels", []):
                if not self.channel_allowed(ch):
                    continue
                logger.info(f"Stahuji historii: #{ch.name} ({ch.id})")
                try:
                    async for msg in ch.history(limit=self.history_limit):
                        if self.author_allowed(msg.author):
                            all_records.append(message_to_record(msg))
                except discord.Forbidden:
                    logger.error(f"Chybí oprávnění pro #{ch.name} ({ch.id}).")
                except Exception as e:
                    logger.error(f"Chyba při čtení #{ch.name}: {e}")

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        save_json(all_records, os.path.join(self.out_dir, f"export_{ts}.json"))
        save_csv(all_records, os.path.join(self.out_dir, f"export_{ts}.csv"))
        logger.info("Aktivní export dokončen.")

    async def on_message(self, message):
        if message.author == self.user:
            return
        if not self.channel_allowed(message.channel):
            return
        if not self.author_allowed(message.author):
            return

        r = message_to_record(message)
        date_key = message.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
        out_json = os.path.join(self.out_dir, f"live_{date_key}.json")

        try:
            with open(out_json, "r", encoding="utf-8") as f:
                existing = json.load(f)
                if not isinstance(existing, list):
                    existing = []
        except Exception:
            existing = []

        existing.append(r)
        save_json(existing, out_json)
        logger.info(f"Zachycena nová zpráva ({r['id']}) -> {out_json}")


def build_intents():
    it = discord.Intents.default()
    it.guilds = True
    it.messages = True
    it.message_content = True
    return it


async def runner(bot: ExtractorBot):
    # ensures aiohttp session is closed properly even on Ctrl+C
    async with bot:
        await bot.start(TOKEN)


async def runner(bot: ExtractorBot):
    try:
        await bot.start(TOKEN, reconnect=False)
    finally:
        if not bot.is_closed():
            await bot.close()
        await asyncio.sleep(0)

def main():
    p = argparse.ArgumentParser(description="Discord Data Extractor")
    p.add_argument("--mode", choices=["passive", "active", "both"], default="both",help="Režim běhu: passive, active nebo both")
    p.add_argument("--out", default="./exports", help="Výstupní složka")
    p.add_argument("--channels", nargs="*", help="Seznam kanálů (ID nebo názvy) pro filtraci")
    p.add_argument("--history-limit", type=int, default=10, help="Počet zpráv stažených na kanál v aktivním režimu")
    p.add_argument("--users", nargs="*", help="Uživatelé (ID / mention <@id>) pro filtraci autorů")
    p.add_argument("--pick-channels", action="store_true", help="Interaktivně vypíše kanály a vybereš (ID/čísla)")
    p.add_argument("--pick-users", action="store_true", help="Interaktivně vybereš uživatele z autorů zpráv (ID/čísla)")
    p.add_argument("--scan-limit", type=int, default=None, help="Kolik zpráv/kanál skenovat pro výpis uživatelů (default = history-limit)")
    p.add_argument("--guild", help="Volitelně cílový server (Guild ID); pokud není, vezme první dostupný")
    a = p.parse_args()

    if a.pick_channels and a.channels:
        return logger.error("Nepoužívej zároveň --pick-channels a --channels.")
    if a.pick_users and a.users:
        return logger.error("Nepoužívej zároveň --pick-users a --users.")
    if not TOKEN:
        return logger.error("Nebyl nalezen DISCORD_TOKEN v .env! Ukončuji.")

    bot = ExtractorBot(
        out_dir=a.out,
        mode=a.mode,
        channels=a.channels,
        users=a.users,
        pick_channels=a.pick_channels,
        pick_users=a.pick_users,
        history_limit=a.history_limit,
        scan_limit=a.scan_limit,
        guild_id=a.guild,
        intents=build_intents(),
    )

    try:
        asyncio.run(runner(bot))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()