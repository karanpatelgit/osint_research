#!/usr/bin/env python3
"""
OSINT Deep Briefing Bot
========================
Auto-scrapes URLs, sends to Claude API, outputs a full investigative briefing.
Usage:
    python osint_bot.py --target "Entity Name" --urls url1 url2 url3
    python osint_bot.py --target "Entity Name" --file urls.txt
    python osint_bot.py --target "Entity Name" --urls url1 --extra "any extra text or context"
"""

import os
import sys
import re
import json
import time
import argparse
import textwrap
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
import anthropic
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
from rich.text import Text
from rich.rule import Rule
from rich.table import Table
from rich import print as rprint

console = Console()

# ─────────────────────────────────────────────────────────────────────────────
# SYSTEM PROMPT — The Investigative Journalist persona
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an elite Investigative Journalist and OSINT (Open-Source Intelligence) Expert. Your job is to dissect the provided raw internet research data about the target entity and expose the hidden network, institutional backers, and potential double standards. Do not fall for public relations (PR) spin, satire covers, or surface-level explanations. Be highly analytical, skeptical, and precise.

Analyze the raw text provided and generate a "Deep Intelligence Briefing" structured exactly under these four headers. Use bullet points for high scannability.

### 1. THE REIGNING CONTEXT & COVERT IDENTITY
- Analyze what this entity claims to be on the surface or what people see (e.g., a meme page, a neutral NGO, a new startup).
- Contrast it immediately with the actual underlying objective or hidden agenda suggested by the research data or what can be a digging point.
- Flag the exact launch timeline or triggers and flag the events: when, why, who, how, and until when it is going on (e.g., "Founded right after X political event").

### 2. THE HUMAN NETWORK & AFFILIATIONS (THE KUNDALI)
- Extract all names of founders, co-founders, key directors, or high-profile promoters and linked persons mentioned in any related article.
- Map out their past and present political connections or any type of connections, institutional roles, corporate ties, or previous controversial ventures.
- Highlight any shared history between these individuals and known political strategists or powerful organizations and also check for other angles.

### 3. THE PAPER TRAIL & MONEY TRAIL
- Document any mentions of funding sources, parent companies, international backing, corporate registrations, or NGO channels — even the exact context mentioned.
- If explicit funding data is missing, identify the operational infrastructure and viewpoint (e.g., who owns the domain, who runs the physical office, which agency managed their launch campaign).

### 4. SMOKING GUNS & CONTRADICTIONS
- Cross-reference the entity's current public stance with the past actions, historical posts, or statements of its founders.
- Highlight any doubtful elements, sudden shifts in narrative, deleted post leaks, or hypocrisies.

---
CRITICAL COMPLIANCE RULES:
1. STRICT FACTUAL ACCURACY: You are a journalist; legal risks are high. You MUST ONLY use facts directly supported by the provided raw text. Do not assume or hallucinate. Provide all references if you are taking any context.
2. EXPLICIT CITATIONS: At the end of every single bullet point, you MUST insert the exact source marker in brackets like [Source 0] or [Source 2], matching the source index provided.
3. UNKNOWN HANDLING: If funding or network info is completely absent from the text, explicitly state: "NO DIRECT PAPER TRAIL FOUND IN CURRENT DATA" instead of making up possibilities.

Respond ONLY with a valid JSON object with exactly these four keys: "context", "network", "trail", "guns".
Each key maps to an array of bullet point strings (plain text, no markdown inside strings).
No markdown fences, no preamble, no explanation — pure JSON only."""


# ─────────────────────────────────────────────────────────────────────────────
# SCRAPER
# ─────────────────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

def scrape_url(url: str, index: int, timeout: int = 15) -> dict:
    """Scrape a single URL and return structured result."""
    result = {
        "index": index,
        "url": url,
        "domain": urlparse(url).netloc,
        "title": "",
        "text": "",
        "status": "ok",
        "error": None,
        "char_count": 0,
    }
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Remove junk
        for tag in soup(["script", "style", "nav", "footer", "header",
                          "aside", "form", "noscript", "iframe", "ads",
                          "[class*='cookie']", "[id*='cookie']"]):
            tag.decompose()

        result["title"] = soup.title.string.strip() if soup.title else url

        # Extract main content — prefer <article> or <main>, fallback to <body>
        main = soup.find("article") or soup.find("main") or soup.find("body")
        if main:
            paragraphs = main.find_all(["p", "h1", "h2", "h3", "h4", "li", "blockquote", "td", "th"])
            lines = []
            for p in paragraphs:
                text = p.get_text(separator=" ", strip=True)
                if len(text) > 30:  # skip tiny fragments
                    lines.append(text)
            result["text"] = "\n".join(lines)
        else:
            result["text"] = soup.get_text(separator="\n", strip=True)

        # Truncate to ~15k chars per source to avoid token bloat
        result["text"] = result["text"][:15000]
        result["char_count"] = len(result["text"])

    except requests.exceptions.Timeout:
        result["status"] = "timeout"
        result["error"] = f"Request timed out after {timeout}s"
    except requests.exceptions.HTTPError as e:
        result["status"] = "http_error"
        result["error"] = str(e)
    except requests.exceptions.ConnectionError:
        result["status"] = "connection_error"
        result["error"] = "Could not connect to host"
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)

    return result


def scrape_all(urls: list[str]) -> list[dict]:
    """Scrape all URLs with progress display."""
    results = []
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold red]Scraping[/bold red] {task.description}"),
        BarColumn(),
        TextColumn("[dim]{task.completed}/{task.total}[/dim]"),
        console=console,
    ) as progress:
        task = progress.add_task("sources...", total=len(urls))
        for i, url in enumerate(urls):
            progress.update(task, description=f"[dim]{urlparse(url).netloc}[/dim]")
            result = scrape_url(url, i)
            results.append(result)
            time.sleep(0.8)  # polite crawl delay
            progress.advance(task)
    return results


# ─────────────────────────────────────────────────────────────────────────────
# CLAUDE ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────

def build_user_message(target: str, scraped: list[dict], extra: str = "") -> str:
    """Build the user message with all scraped sources labeled."""
    lines = [f"TARGET ENTITY: {target}", "", "RAW RESEARCH DATA:", ""]

    for s in scraped:
        lines.append(f"[Source {s['index']}] URL: {s['url']}")
        lines.append(f"Domain: {s['domain']}")
        if s["title"]:
            lines.append(f"Title: {s['title']}")
        if s["status"] == "ok":
            lines.append(f"Content ({s['char_count']} chars):")
            lines.append(s["text"])
        else:
            lines.append(f"[SCRAPE FAILED: {s['error']}]")
        lines.append("")
        lines.append("─" * 60)
        lines.append("")

    if extra.strip():
        lines.append(f"[Extra Context / Manual Notes]")
        lines.append(extra.strip())
        lines.append("")

    lines.append("Generate the full Deep Intelligence Briefing JSON now.")
    return "\n".join(lines)


def run_analysis(target: str, user_message: str, api_key: str) -> dict:
    """Send to Claude API and parse JSON response."""
    client = anthropic.Anthropic(api_key=api_key)

    console.print("\n[bold red]◌[/bold red] Transmitting to Claude API...", end=" ")

    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    console.print("[green]RECEIVED[/green]")

    raw_text = "".join(
        block.text for block in message.content if hasattr(block, "text")
    )

    # Strip markdown fences if model disobeyed
    clean = re.sub(r"```json|```", "", raw_text).strip()

    try:
        parsed = json.loads(clean)
    except json.JSONDecodeError:
        # Try to extract JSON object
        match = re.search(r"\{[\s\S]*\}", clean)
        if match:
            parsed = json.loads(match.group())
        else:
            raise ValueError(f"Could not parse model response as JSON.\nRaw:\n{clean[:500]}")

    return parsed, message.usage


# ─────────────────────────────────────────────────────────────────────────────
# OUTPUT RENDERER
# ─────────────────────────────────────────────────────────────────────────────

SECTION_META = [
    ("01", "context", "THE REIGNING CONTEXT & COVERT IDENTITY",      "red"),
    ("02", "network", "THE HUMAN NETWORK & AFFILIATIONS (THE KUNDALI)", "yellow"),
    ("03", "trail",   "THE PAPER TRAIL & MONEY TRAIL",                "cyan"),
    ("04", "guns",    "SMOKING GUNS & CONTRADICTIONS",                "magenta"),
]

def render_terminal(target: str, briefing: dict, sources: list[dict], usage):
    """Pretty-print the briefing to terminal using Rich."""
    console.print()
    console.rule("[bold red]DEEP INTELLIGENCE BRIEFING[/bold red]", style="red")
    console.print(
        Panel(
            f"[bold white]ENTITY:[/bold white] {target}\n"
            f"[dim]Generated: {datetime.now().strftime('%d %b %Y · %H:%M:%S')}  |  "
            f"Tokens used: {usage.input_tokens} in / {usage.output_tokens} out[/dim]",
            border_style="red",
            expand=True,
        )
    )
    console.print()

    for num, key, title, color in SECTION_META:
        bullets = briefing.get(key, ["No data found for this section."])
        console.print(f"[bold {color}]### {num}. {title}[/bold {color}]")
        console.print()
        for bullet in bullets:
            # Highlight source tags
            formatted = re.sub(r"\[Source (\d+)\]", r"[dim cyan]\[Source \1\][/dim cyan]", bullet)
            formatted = formatted.replace(
                "NO DIRECT PAPER TRAIL FOUND IN CURRENT DATA",
                "[bold yellow]⚑ NO DIRECT PAPER TRAIL FOUND IN CURRENT DATA[/bold yellow]"
            )
            # Word-wrap each bullet
            wrapped = textwrap.fill(formatted, width=110, subsequent_indent="  ")
            console.print(f"  [bold {color}]▸[/bold {color}] {wrapped}")
            console.print()
        console.rule(style="dim")
        console.print()

    # Source table
    console.print("[bold]SOURCES SCRAPED[/bold]")
    table = Table(show_header=True, header_style="bold dim", box=None, padding=(0, 2))
    table.add_column("ID", style="cyan", width=6)
    table.add_column("Domain", style="white", width=30)
    table.add_column("Status", width=14)
    table.add_column("Chars", justify="right", width=8)
    table.add_column("URL", style="dim", max_width=60)

    for s in sources:
        status_str = "[green]OK[/green]" if s["status"] == "ok" else f"[red]{s['status']}[/red]"
        table.add_row(
            f"[{s['index']}]",
            s["domain"],
            status_str,
            str(s["char_count"]) if s["char_count"] else "—",
            s["url"],
        )
    console.print(table)
    console.print()


def save_markdown(target: str, briefing: dict, sources: list[dict], output_dir: Path) -> Path:
    """Save the briefing as a Markdown file."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_target = re.sub(r"[^\w\s-]", "", target).strip().replace(" ", "_")[:40]
    filename = f"briefing_{safe_target}_{timestamp}.md"
    filepath = output_dir / filename

    lines = [
        f"# DEEP INTELLIGENCE BRIEFING",
        f"",
        f"**Entity:** {target}  ",
        f"**Generated:** {datetime.now().strftime('%d %b %Y at %H:%M:%S')}  ",
        f"**Classification:** CONFIDENTIAL — FOR RESEARCH USE ONLY",
        f"",
        f"---",
        f"",
    ]

    for num, key, title, _ in SECTION_META:
        lines.append(f"## {num}. {title}")
        lines.append("")
        for bullet in briefing.get(key, ["No data found."]):
            lines.append(f"- {bullet}")
        lines.append("")
        lines.append("---")
        lines.append("")

    lines.append("## SOURCES")
    lines.append("")
    for s in sources:
        status = "✓" if s["status"] == "ok" else "✗"
        lines.append(f"- **[Source {s['index']}]** {status} [{s['domain']}]({s['url']})")
        if s["error"]:
            lines.append(f"  - Error: {s['error']}")
    lines.append("")
    lines.append("---")
    lines.append("*This briefing was generated by the OSINT Deep Briefing Bot. All facts are sourced strictly from the provided URLs. Verify independently before publication.*")

    filepath.write_text("\n".join(lines), encoding="utf-8")
    return filepath


def save_json(target: str, briefing: dict, sources: list[dict], output_dir: Path) -> Path:
    """Save raw JSON output."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_target = re.sub(r"[^\w\s-]", "", target).strip().replace(" ", "_")[:40]
    filename = f"briefing_{safe_target}_{timestamp}.json"
    filepath = output_dir / filename

    payload = {
        "target": target,
        "generated_at": datetime.now().isoformat(),
        "briefing": briefing,
        "sources": sources,
    }
    filepath.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return filepath


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="OSINT Deep Briefing Bot — auto-scrape URLs and generate an investigative briefing via Claude.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
        Examples:
          python osint_bot.py --target "XYZ NGO" --urls https://example.com https://news.com/article
          python osint_bot.py --target "ABC Media" --file urls.txt
          python osint_bot.py --target "StartupXYZ" --urls https://crunchbase.com/... --extra "Founded 2021, Delhi"
          python osint_bot.py --target "PoliticalPage" --urls https://... --json --no-markdown
        """),
    )
    parser.add_argument("--target", required=True, help="Name of the entity being investigated")
    parser.add_argument("--urls", nargs="+", metavar="URL", help="One or more URLs to scrape")
    parser.add_argument("--file", metavar="FILE", help="Text file with one URL per line")
    parser.add_argument("--extra", default="", metavar="TEXT", help="Extra manual context/notes to append")
    parser.add_argument("--output-dir", default="./output", metavar="DIR", help="Directory to save reports (default: ./output)")
    parser.add_argument("--api-key", metavar="KEY", help="Anthropic API key (or set ANTHROPIC_API_KEY env var)")
    parser.add_argument("--json", action="store_true", dest="save_json", help="Also save raw JSON output")
    parser.add_argument("--no-markdown", action="store_true", help="Skip saving Markdown report")
    parser.add_argument("--timeout", type=int, default=15, help="Scrape timeout per URL in seconds (default: 15)")
    return parser.parse_args()


def main():
    args = parse_args()

    # ── Banner ────────────────────────────────────────────────────────────────
    console.print()
    console.print(Panel(
        "[bold red]OSINT DEEP BRIEFING ENGINE[/bold red]\n"
        "[dim]Investigative Analysis · Follow the Money · Expose the Network[/dim]",
        border_style="red",
        expand=False,
    ))
    console.print()

    # ── API key ───────────────────────────────────────────────────────────────
    api_key = args.api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        console.print("[bold red]ERROR:[/bold red] No API key found.")
        console.print("Set it via:  [cyan]export ANTHROPIC_API_KEY='sk-ant-...'[/cyan]")
        console.print("Or pass it:  [cyan]--api-key sk-ant-...[/cyan]")
        sys.exit(1)

    # ── Collect URLs ──────────────────────────────────────────────────────────
    urls = list(args.urls or [])
    if args.file:
        file_path = Path(args.file)
        if not file_path.exists():
            console.print(f"[red]ERROR:[/red] File not found: {args.file}")
            sys.exit(1)
        file_urls = [line.strip() for line in file_path.read_text().splitlines()
                     if line.strip() and not line.startswith("#")]
        urls.extend(file_urls)

    if not urls:
        console.print("[red]ERROR:[/red] No URLs provided. Use --urls or --file.")
        sys.exit(1)

    # Deduplicate while preserving order
    seen = set()
    urls = [u for u in urls if not (u in seen or seen.add(u))]

    console.print(f"[bold]Target:[/bold] {args.target}")
    console.print(f"[bold]URLs:[/bold] {len(urls)} source(s)")
    for i, u in enumerate(urls):
        console.print(f"  [dim][{i}][/dim] {u}")
    console.print()

    # ── Scrape ────────────────────────────────────────────────────────────────
    console.rule("[dim]PHASE 1 — SCRAPING[/dim]")
    scraped = scrape_all(urls)

    ok_count = sum(1 for s in scraped if s["status"] == "ok")
    total_chars = sum(s["char_count"] for s in scraped)
    console.print(f"\n[green]✓[/green] Scraped {ok_count}/{len(urls)} sources · {total_chars:,} chars of raw data\n")

    if ok_count == 0:
        console.print("[bold red]ERROR:[/bold red] All URLs failed to scrape. Check your URLs and internet connection.")
        sys.exit(1)

    # ── Build prompt ──────────────────────────────────────────────────────────
    console.rule("[dim]PHASE 2 — ANALYSIS[/dim]")
    user_message = build_user_message(args.target, scraped, args.extra)

    # ── Claude API ────────────────────────────────────────────────────────────
    try:
        briefing, usage = run_analysis(args.target, user_message, api_key)
    except Exception as e:
        console.print(f"\n[bold red]API ERROR:[/bold red] {e}")
        sys.exit(1)

    # ── Render to terminal ────────────────────────────────────────────────────
    console.rule("[dim]PHASE 3 — BRIEFING[/dim]")
    render_terminal(args.target, briefing, scraped, usage)

    # ── Save files ────────────────────────────────────────────────────────────
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    saved_files = []

    if not args.no_markdown:
        md_path = save_markdown(args.target, briefing, scraped, output_dir)
        saved_files.append(("Markdown Report", md_path))

    if args.save_json:
        json_path = save_json(args.target, briefing, scraped, output_dir)
        saved_files.append(("JSON Data", json_path))

    if saved_files:
        console.print("[bold]Files saved:[/bold]")
        for label, path in saved_files:
            console.print(f"  [green]✓[/green] {label}: [cyan]{path.resolve()}[/cyan]")
        console.print()

    console.rule("[dim red]END OF BRIEFING[/dim red]", style="red")
    console.print()


if __name__ == "__main__":
    main()
