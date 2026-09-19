"""Read-only loaders for AMI NXT manual annotations (words, dialogue acts, summaries, topics)."""
import re
from pathlib import Path
from xml.etree import ElementTree as ET

ANN = Path("D:/coding/whisper_local/benchmarks/meeting_mode/data/ami/annotations")
NS = "{http://nite.sourceforge.net/}"
NID = NS + "id"
DA_TYPES = {
    "ami_da_1": "bck", "ami_da_2": "stl", "ami_da_3": "fra", "ami_da_4": "inf", "ami_da_5": "el.inf",
    "ami_da_6": "sug", "ami_da_7": "off", "ami_da_8": "el.sug", "ami_da_9": "ass", "ami_da_11": "el.ass",
    "ami_da_12": "und", "ami_da_13": "el.und", "ami_da_14": "be.pos", "ami_da_15": "be.neg", "ami_da_16": "oth",
}
COLLAPSE = {
    "inf": "inform", "sug": "suggest", "off": "offer", "ass": "assess",
    "el.inf": "elicit", "el.sug": "elicit", "el.ass": "elicit", "el.und": "elicit",
    "be.pos": "social", "be.neg": "social", "und": "understanding",
    "bck": "minor", "stl": "minor", "fra": "minor", "oth": "other",
}
_RANGE = re.compile(r"([^#]+)#id\(([^)]+)\)(?:\.\.id\(([^)]+)\))?")


def parse_href(href):
    m = _RANGE.match(href)
    return m.group(1), m.group(2), m.group(3) or m.group(2)


def roles(mid):
    root = ET.parse(ANN / "corpusResources" / "meetings.xml").getroot()
    for meeting in root.iter("meeting"):
        if meeting.get("observation") == mid:
            return {s.get("nxt_agent"): s.get("role") for s in meeting.iter("speaker")}
    return {}


def topic_names():
    root = ET.parse(ANN / "ontologies" / "default-topics.xml").getroot()
    return {node.get(NID): node.get("name") for node in root.iter() if node.get(NID)}


class Words:
    """All nodes of one speaker's words file in file order, with id -> index."""

    def __init__(self, path):
        self.nodes = []
        self.index = {}
        for node in ET.parse(path).getroot():
            nid = node.get(NID)
            if not nid:
                continue
            kind = node.tag.rsplit("}", 1)[-1]
            self.index[nid] = len(self.nodes)
            self.nodes.append({
                "id": nid, "kind": kind, "punc": node.get("punc") == "true",
                "text": (node.text or "").strip(), "start": _f(node.get("starttime")), "end": _f(node.get("endtime")),
            })

    def span(self, first, last):
        return self.nodes[self.index[first]: self.index[last] + 1]


def _f(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def render(nodes):
    out = []
    for n in nodes:
        if n["kind"] != "w" or not n["text"]:
            continue
        if n["punc"] and out:
            out[-1] += n["text"]
        else:
            out.append(n["text"])
    return " ".join(out)


def timing(nodes):
    starts = [n["start"] for n in nodes if n["kind"] == "w" and n["start"] is not None]
    ends = [n["end"] for n in nodes if n["kind"] == "w" and n["end"] is not None]
    return (min(starts) if starts else None, max(ends) if ends else None)


def load_words(mid):
    return {p.name.split(".")[1]: Words(p) for p in sorted((ANN / "words").glob(f"{mid}.*.words.xml"))}


def load_dialogue_acts(mid, words=None):
    """Return dialogue acts sorted by start time with text, type, speaker, addressee, and word count."""
    words = words or load_words(mid)
    acts = []
    for path in sorted((ANN / "dialogueActs").glob(f"{mid}.*.dialog-act.xml")):
        speaker = path.name.split(".")[1]
        if speaker not in words:
            continue
        for order, dact in enumerate(ET.parse(path).getroot().iter("dact")):
            da_type = None
            nodes = []
            for child in dact:
                tag = child.tag.rsplit("}", 1)[-1]
                if tag == "pointer" and child.get("role") == "da-aspect":
                    da_type = DA_TYPES.get(child.get("href").split("id(")[1].rstrip(")"))
                elif tag == "child":
                    _, first, last = parse_href(child.get("href"))
                    nodes.extend(words[speaker].span(first, last))
            text = render(nodes)
            start, end = timing(nodes)
            if not text or start is None or da_type is None:
                continue
            acts.append({
                "id": dact.get(NID), "speaker": speaker, "order": order, "type": da_type,
                "label": COLLAPSE[da_type], "addressee": dact.get("addressee"), "text": text,
                "start": start, "end": end, "words": len([n for n in nodes if n["kind"] == "w" and not n["punc"]]),
            })
    acts.sort(key=lambda a: (a["start"], a["speaker"]))
    for i, a in enumerate(acts):
        a["index"] = i
    return acts


def load_abstractive(mid):
    """Return abstractive sentences: {id, section, text}."""
    path = ANN / "abstractive" / f"{mid}.abssumm.xml"
    if not path.exists():
        return []
    rows = []
    for section in ET.parse(path).getroot():
        name = section.tag.rsplit("}", 1)[-1]
        for sentence in section.iter("sentence"):
            rows.append({"id": sentence.get(NID), "section": name, "text": (sentence.text or "").strip()})
    return rows


def load_summlinks(mid):
    """Return {abstract sentence id: set(dialogue act ids)}."""
    path = ANN / "extractive" / f"{mid}.summlink.xml"
    links = {}
    if not path.exists():
        return links
    for link in ET.parse(path).getroot().iter("summlink"):
        ext = abs_id = None
        for p in link.iter(NS + "pointer"):
            if p.get("role") == "extractive":
                ext = p.get("href").split("id(")[1].rstrip(")")
            elif p.get("role") == "abstractive":
                abs_id = p.get("href").split("id(")[1].rstrip(")")
        if ext and abs_id:
            links.setdefault(abs_id, set()).add(ext)
    return links


def load_extractive(mid, acts):
    """Return the set of dialogue-act ids marked as extractive summary."""
    path = ANN / "extractive" / f"{mid}.extsumm.xml"
    chosen = set()
    if not path.exists():
        return chosen
    by_file = {}
    for a in acts:
        by_file.setdefault(a["speaker"], []).append(a)
    order_index = {a["id"]: (a["speaker"], a["order"]) for a in acts}
    for child in ET.parse(path).getroot().iter(NS + "child"):
        _, first, last = parse_href(child.get("href"))
        if first not in order_index or last not in order_index:
            continue
        speaker, lo = order_index[first]
        _, hi = order_index[last]
        for a in by_file[speaker]:
            if lo <= a["order"] <= hi:
                chosen.add(a["id"])
    return chosen


def load_topics(mid, words=None):
    """Return top-level and leaf topic spans: {label, start, end, depth}."""
    words = words or load_words(mid)
    names = topic_names()
    path = ANN / "topics" / f"{mid}.topic.xml"
    rows = []
    if not path.exists():
        return rows

    def walk(node, depth):
        added = []
        for topic in node:
            if topic.tag != "topic":
                continue
            label = topic.get("other_description")
            nodes = []
            for child in topic:
                tag = child.tag.rsplit("}", 1)[-1]
                if tag == "pointer" and not label:
                    label = names.get(child.get("href").split("id(")[1].rstrip(")"))
                elif tag == "child":
                    file, first, last = parse_href(child.get("href"))
                    speaker = file.split(".")[1]
                    if speaker in words:
                        nodes.extend(words[speaker].span(first, last))
            start, end = timing(nodes)
            sub = walk(topic, depth + 1)
            if sub:
                start = min([s for s in [start] + [x["start"] for x in sub] if s is not None], default=None)
                end = max([e for e in [end] + [x["end"] for x in sub] if e is not None], default=None)
            if start is not None:
                row = {"label": label or "other", "start": start, "end": end, "depth": depth}
                rows.append(row)
                added.append(row)
        return added

    walk(ET.parse(path).getroot(), 0)
    rows.sort(key=lambda r: (r["start"], r["depth"]))
    return rows


def speaker_tag(speaker, role_map):
    role = role_map.get(speaker)
    return f"{speaker} ({role})" if role else speaker


if __name__ == "__main__":
    import json
    mid = "ES2008a"
    w = load_words(mid)
    acts = load_dialogue_acts(mid, w)
    print(mid, "acts", len(acts), "roles", roles(mid))
    from collections import Counter
    print(Counter(a["label"] for a in acts))
    for a in acts[:8]:
        print(json.dumps({k: a[k] for k in ("speaker", "type", "label", "addressee", "text", "start")})[:200])
    abs_rows = load_abstractive(mid)
    links = load_summlinks(mid)
    print("abstract sentences", len(abs_rows), "linked", len(links))
    by_id = {a["id"]: a for a in acts}
    s = abs_rows[6]
    print("SENTENCE:", s)
    for did in sorted(links.get(s["id"], []))[:5]:
        print("   ->", by_id[did]["speaker"], by_id[did]["text"][:120])
    ext = load_extractive(mid, acts)
    print("extractive acts", len(ext), "of", len(acts))
    for t in load_topics(mid, w):
        print("TOPIC", t)
