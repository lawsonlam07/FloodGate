"""Spoken "radio bulletins" of an incident's key facts, voiced by ElevenLabs.

The script is built by rules from the board's key facts; no LLM writes it, so the bulletin can never add advice:
  - in the language of the source pages it reads the official sentences word for word (verbatim quotes);
  - in English (for a non-English incident) it reads the board's own English lines, the same text shown on screen.
Every bulletin about a fictional scenario starts by saying so. Real FMI warnings read FMI's own description.

Audio is cached per script (data/audio/<hash>.mp3), so each state of the board is voiced at most once per language;
generation is capped per day (ELEVENLABS_CHAR_BUDGET_DAILY) and blocked while processing is paused.

Settings: ELEVENLABS_API_KEY, ELEVENLABS_VOICE (default a calm newsreader voice), ELEVENLABS_MODEL
(default eleven_flash_v2_5: fast, multilingual), ELEVENLABS_CHAR_BUDGET_DAILY (default 20000).
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

from .config import ROOT, SCENARIO_SPECS

AUDIO_DIR = ROOT / "data" / "audio"
USAGE_FILE = AUDIO_DIR / "usage.json"
VOICE = os.environ.get("ELEVENLABS_VOICE", "onwK4e9ZLuTAKqWW03F9")
MODEL = os.environ.get("ELEVENLABS_MODEL", "eleven_flash_v2_5")
CHAR_BUDGET_DAILY = int(os.environ.get("ELEVENLABS_CHAR_BUDGET_DAILY", "20000"))
MAX_CHARS = 1400

# Fixed phrases per language (no machine translation at run time). Native-speaker review still welcome.
PHRASES = {
    "en": ("Fictional exercise. This is not a real emergency.", "FloodGate situation update for {place}.",
           "Confirmed by official sources:", "Official instructions:", "Not true, according to officials:",
           "Unconfirmed reports. Do not act on them yet:", "Sources for every line are on the FloodGate board.",
           "No official confirmation yet. Reports are being checked."),
    "de": ("Fiktive Übung. Dies ist kein echter Notfall.", "FloodGate-Lagemeldung für {place}.",
           "Von den Behörden bestätigt:", "Anweisungen der Behörden:", "Laut den Behörden nicht wahr:",
           "Unbestätigte Meldungen. Bitte noch nicht danach handeln:", "Die Quellen zu jeder Zeile stehen im FloodGate-Lagebild.",
           "Noch keine offizielle Bestätigung. Die Meldungen werden geprüft."),
    "fr": ("Exercice fictif. Il ne s'agit pas d'une véritable urgence.", "Point de situation FloodGate pour {place}.",
           "Confirmé par les autorités :", "Consignes officielles :", "Faux, selon les autorités :",
           "Informations non confirmées. N'agissez pas encore en conséquence :", "Les sources de chaque ligne sont sur le tableau FloodGate.",
           "Aucune confirmation officielle pour l'instant. Les informations sont en cours de vérification."),
    "nl": ("Fictieve oefening. Dit is geen echte noodsituatie.", "FloodGate-situatie-update voor {place}.",
           "Bevestigd door de autoriteiten:", "Officiële instructies:", "Niet waar, volgens de autoriteiten:",
           "Onbevestigde berichten. Handel er nog niet naar:", "De bronnen van elke regel staan op het FloodGate-bord.",
           "Nog geen officiële bevestiging. De berichten worden gecontroleerd."),
    "pl": ("Ćwiczenie fikcyjne. To nie jest prawdziwy stan zagrożenia.", "Aktualizacja sytuacji FloodGate: {place}.",
           "Potwierdzone przez władze:", "Oficjalne zalecenia:", "Nieprawda, według władz:",
           "Niepotwierdzone doniesienia. Nie działaj jeszcze na ich podstawie:", "Źródła każdej informacji są na tablicy FloodGate.",
           "Brak oficjalnego potwierdzenia. Doniesienia są sprawdzane."),
    "sv": ("Fiktiv övning. Detta är ingen verklig nödsituation.", "FloodGate-lägesrapport för {place}.",
           "Bekräftat av myndigheterna:", "Myndigheternas uppmaningar:", "Inte sant, enligt myndigheterna:",
           "Obekräftade uppgifter. Agera inte på dem ännu:", "Källorna till varje rad finns på FloodGate-tavlan.",
           "Ingen officiell bekräftelse ännu. Uppgifterna kontrolleras."),
    "cs": ("Smyšlené cvičení. Nejde o skutečnou mimořádnou událost.", "Aktuální situace FloodGate: {place}.",
           "Potvrzeno úřady:", "Oficiální pokyny:", "Podle úřadů není pravda:",
           "Nepotvrzené zprávy. Zatím podle nich nejednejte:", "Zdroje ke každému řádku najdete na tabuli FloodGate.",
           "Zatím bez oficiálního potvrzení. Zprávy se ověřují."),
    "pt": ("Exercício fictício. Não se trata de uma emergência real.", "Ponto de situação FloodGate para {place}.",
           "Confirmado pelas autoridades:", "Instruções oficiais:", "Falso, segundo as autoridades:",
           "Informações não confirmadas. Não aja ainda com base nelas:", "As fontes de cada linha estão no painel FloodGate.",
           "Ainda sem confirmação oficial. As informações estão a ser verificadas."),
    "it": ("Esercitazione fittizia. Non si tratta di una vera emergenza.", "Aggiornamento FloodGate per {place}.",
           "Confermato dalle autorità:", "Indicazioni ufficiali:", "Non è vero, secondo le autorità:",
           "Notizie non confermate. Non agite ancora di conseguenza:", "Le fonti di ogni riga sono sulla bacheca FloodGate.",
           "Nessuna conferma ufficiale per ora. Le notizie sono in corso di verifica."),
    "es": ("Simulacro ficticio. No se trata de una emergencia real.", "Actualización de FloodGate para {place}.",
           "Confirmado por las autoridades:", "Instrucciones oficiales:", "Falso, según las autoridades:",
           "Informaciones sin confirmar. No actúe todavía en consecuencia:", "Las fuentes de cada línea están en el panel de FloodGate.",
           "Todavía no hay confirmación oficial. Las informaciones se están verificando."),
    "el": ("Φανταστική άσκηση. Δεν πρόκειται για πραγματική έκτακτη ανάγκη.", "Ενημέρωση FloodGate για {place}.",
           "Επιβεβαιωμένα από τις αρχές:", "Επίσημες οδηγίες:", "Δεν ισχύει, σύμφωνα με τις αρχές:",
           "Ανεπιβεβαίωτες πληροφορίες. Μην ενεργήσετε ακόμη βάσει αυτών:", "Οι πηγές κάθε γραμμής βρίσκονται στον πίνακα FloodGate.",
           "Δεν υπάρχει ακόμη επίσημη επιβεβαίωση. Οι πληροφορίες ελέγχονται."),
}
FICTIONAL, INTRO, CONFIRMED, TODO, NOT_TRUE, DONT_ACT, OUTRO, NOTHING_YET = range(8)


def source_language(incident_id: str) -> str:
    if incident_id.startswith("halvik"):
        return "en"
    return (SCENARIO_SPECS.get(incident_id) or {}).get("language", "en")


def languages(incident: dict) -> list[str]:
    """Bulletin languages offered for an incident: English plus the language of its pages, if different."""
    if incident.get("kind") == "official_warning":
        return ["en"]
    lang = source_language(incident["id"])
    return ["en"] + ([lang] if lang != "en" and lang in PHRASES else [])


def _sentence(text: str) -> str:
    text = " ".join((text or "").split()).strip(" ,;:").lstrip("-*•·– ")
    if not text:
        return text
    text = text[0].upper() + text[1:]                     # quotes can start mid-sentence
    return text if text[-1] in ".!?…" else text + "."


def _quote(item: dict, stance: str) -> str | None:
    """The original words for an item: an official source first, then any source with that stance."""
    quotes = [x for x in item.get("sources") or [] if x.get("stance") == stance and x.get("evidence_quote")]
    quotes.sort(key=lambda x: x.get("kind") != "official")
    return quotes[0]["evidence_quote"] if quotes else None


def script(detail: dict, lang: str = "en") -> dict:
    """Rule-built bulletin text for an incident detail (store.incident_detail)."""
    inc, facts = detail["incident"], detail.get("key_facts") or {}
    p = PHRASES.get(lang, PHRASES["en"])
    lines: list[tuple[str, str]] = []

    if inc.get("kind") == "official_warning":
        publisher = ((inc.get("sources") or [{}])[0].get("name")) or inc.get("source_label") or "an official source"
        lines.append(("intro", f"Official warning from {publisher}."))
        lines.append(("fact", _sentence(inc.get("summary") or inc.get("name") or "")))
        if not ((inc.get("sources") or [{}])[0].get("has_instruction")):          # the feed gave no advice itself
            for x in ((inc.get("official_page") or {}).get("instructions") or [])[:3]:   # read via Context.dev
                lines.append(("fact", _sentence(x["text"])))
        until = (inc.get("valid") or {}).get("to")
        if until:
            try:
                if not inc["id"].startswith("fmi-"):
                    raise LookupError                 # only FMI is local to one time zone
                tz, zone = ZoneInfo("Europe/Helsinki"), "Finnish time"
            except Exception:                         # slim containers may lack the time zone database
                tz, zone = timezone.utc, "UTC"
            t = datetime.fromisoformat(until.replace("Z", "+00:00")).astimezone(tz)
            lines.append(("fact", f"Valid until {t:%H:%M} {zone}, {t.day} {t:%B}."))
        lines.append(("outro", f"Source: {publisher}'s official feed, shown on the FloodGate board."))
    else:
        verbatim = lang == source_language(inc["id"])        # same language as the pages: read the officials' words
        items = {it["id"]: it for it in detail.get("items") or []}
        place = (inc.get("area") or {}).get("label") or inc.get("name")
        place = place.replace("(fictional)", "").strip()

        def say(entry: dict, stance: str = "asserts") -> str | None:
            if not verbatim:
                return _sentence(entry["title"])
            quote = _quote(items.get(entry["item_id"]) or {}, stance)
            return _sentence(quote) if quote else None          # never mix in English lines

        lines.append(("intro", p[FICTIONAL]))
        lines.append(("intro", p[INTRO].format(place=place)))
        sections = [
            (CONFIRMED, [say(e) for e in facts.get("confirmed", [])[:4]]),     # superseded warnings already dropped
            (TODO, [say(e) for e in facts.get("what_to_do", [])[:4]]),
            (NOT_TRUE, [say(e, "denies") for e in facts.get("not_true", [])[:3]]),
            # held-back claims come from posts; in English the board's line reads better than a first-person quote
            (DONT_ACT, [_sentence(e["title"]) if lang == "en" else say(e) for e in facts.get("dont_act_yet", [])[:2]]),
        ]
        if not any(texts for _, texts in sections):
            lines.append(("heading", p[NOTHING_YET]))
        for heading, texts in sections:
            texts = list(dict.fromkeys(t for t in texts if t))          # the same quote can back several facts
            if texts:
                lines.append(("heading", p[heading]))
                lines += [("fact", t) for t in texts]
        lines.append(("outro", p[OUTRO]))

    out, total = [], 0
    for kind, text in lines:                                  # keep it short: drop facts beyond the length cap
        if kind == "fact" and total + len(text) > MAX_CHARS:
            continue
        out.append({"kind": kind, "text": text})
        total += len(text) + 1
    text = "\n".join(line["text"] for line in out)
    digest = hashlib.sha1(json.dumps([text, lang, VOICE, MODEL]).encode()).hexdigest()[:20]
    return {"lang": lang, "lines": out, "text": text, "chars": len(text), "id": digest,
            "verbatim": inc.get("kind") != "official_warning" and lang == source_language(inc["id"])}


# ---- audio -------------------------------------------------------------------------------------------------------

def audio_path(digest: str):
    return AUDIO_DIR / f"{digest}.mp3"


def chars_used_today() -> int:
    try:
        usage = json.loads(USAGE_FILE.read_text())
    except (OSError, ValueError):
        return 0
    return usage.get("chars", 0) if usage.get("day") == datetime.now(timezone.utc).date().isoformat() else 0


def _add_usage(chars: int) -> None:
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    USAGE_FILE.write_text(json.dumps({"day": datetime.now(timezone.utc).date().isoformat(),
                                      "chars": chars_used_today() + chars}))


class TTSError(RuntimeError):
    pass


def synthesize(bulletin: dict) -> tuple[str, int]:
    """Blocking. Voice the bulletin with ElevenLabs (or reuse the cached file). Returns (path, characters billed)."""
    path = audio_path(bulletin["id"])
    if path.exists():
        return str(path), 0
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        raise TTSError("ELEVENLABS_API_KEY not set")
    if CHAR_BUDGET_DAILY and chars_used_today() + bulletin["chars"] > CHAR_BUDGET_DAILY:
        raise TTSError(f"daily ElevenLabs budget of {CHAR_BUDGET_DAILY} characters reached")
    r = requests.post(f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE}",
                      headers={"xi-api-key": key}, params={"output_format": "mp3_44100_64"},
                      json={"text": bulletin["text"], "model_id": MODEL, "language_code": bulletin["lang"]},
                      timeout=60)
    if r.status_code != 200 or not r.headers.get("content-type", "").startswith("audio/"):
        raise TTSError(f"ElevenLabs {r.status_code}: {r.text[:200]}")
    cost = int(r.headers.get("character-cost") or bulletin["chars"])
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(r.content)
    tmp.replace(path)
    _add_usage(cost)
    return str(path), cost
