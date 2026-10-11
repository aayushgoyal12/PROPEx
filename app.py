import json
import os
import re
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path

import streamlit as st
from google import genai
from google.genai import types

# ==============================================================================
# 1. CORE TITLE & RISK RULES ENGINE
# ==============================================================================

TRANSFER_TYPES = {
    "SALE_DEED", "GIFT_DEED", "PARTITION_DEED", "RELEASE_DEED", "WILL",
    "SETTLEMENT_DEED", "CONVEYANCE_DEED"
}
RECORD_TYPES = {"LAND_RECORD", "MUTATION", "PROPERTY_TAX"}
DOC_LABELS = {
    "SALE_DEED": "Sale deed", "GIFT_DEED": "Gift deed", "PARTITION_DEED": "Partition deed",
    "RELEASE_DEED": "Release deed", "WILL": "Will / probate", "SETTLEMENT_DEED": "Settlement deed",
    "CONVEYANCE_DEED": "Conveyance deed", "POA": "Power of attorney",
    "ENCUMBRANCE_CERTIFICATE": "Encumbrance certificate", "LAND_RECORD": "Land record (Jamabandi / Fard / 7/12 / RTC)",
    "MUTATION": "Mutation / Intaqal entry", "PROPERTY_TAX": "Property tax receipt / Khata", "OC_CC": "OC / CC",
    "BUILDING_APPROVAL": "Building plan approval", "RERA": "RERA registration", "LOAN_NOC": "Bank NOC / loan closure",
    "OTHER": "Other document",
}
TITLES = {"shri", "sri", "smt", "kum", "mr", "mrs", "ms", "late", "dr", "m/s", "ms.", "s/o", "w/o", "d/o"}
PENALTY = {"RED": 25, "AMBER": 10, "INFO": 2}
AADHAAR = re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b")
PAN = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")


def mask(text):
    if not isinstance(text, str):
        return text
    return PAN.sub(lambda m: m.group(0)[:2] + "XXXXX" + m.group(0)[-1], AADHAAR.sub("XXXX XXXX XXXX", text))


def mask_obj(o):
    if isinstance(o, dict):
        return {k: mask_obj(v) for k, v in o.items()}
    if isinstance(o, list):
        return [mask_obj(v) for v in o]
    return mask(o)


def _norm_name(n):
    toks = [t for t in re.sub(r"[^a-z\s]", " ", (n or "").lower()).split() if t not in TITLES and len(t) > 1]
    return " ".join(sorted(toks))


def same_party(a_list, b_list):
    for a in a_list or []:
        for b in b_list or []:
            na, nb = _norm_name(a), _norm_name(b)
            if na and nb and (na == nb or SequenceMatcher(None, na, nb).ratio() >= 0.8
                              or set(na.split()) <= set(nb.split()) or set(nb.split()) <= set(na.split())):
                return True
    return False


def _year(d):
    try:
        return int(str(d)[:4])
    except (TypeError, ValueError):
        return None


def _norm_id(v):
    m = re.search(r"\d+(?:\s*[/-]\s*\d+)*", str(v or ""))
    return re.sub(r"\s", "", m.group(0)).replace("-", "/") if m else ""


def _flag(level, title, detail, docs=None):
    return {"level": level, "title": title, "detail": detail, "docs": docs or []}


def analyse(docs, today=None):
    today = today or date.today()
    flags = []
    by_type = {}
    for d in docs:
        by_type.setdefault(d.get("doc_type", "OTHER"), []).append(d)

    transfers = sorted([d for d in docs if d.get("doc_type") in TRANSFER_TYPES and d.get("date")],
                       key=lambda d: d["date"])
    chain = []
    for i, d in enumerate(transfers):
        link_ok = i == 0 or same_party(d.get("transferors"), transfers[i - 1].get("transferees"))
        chain.append({
            "doc_id": d["id"], "date": d["date"], "doc_type": d["doc_type"],
            "from": d.get("transferors") or [], "to": d.get("transferees") or [],
            "consideration": d.get("consideration"), "registration_no": d.get("registration_no"),
            "via_poa": bool(d.get("executed_via_poa")), "link_ok": link_ok
        })
        if not link_ok:
            prev = transfers[i - 1]
            flags.append(_flag("RED", "Missing link in chain of title",
                               f"Buyer in {prev['date'][:4]} ({', '.join(prev.get('transferees') or ['?'])}) is not the "
                               f"seller in {d['date'][:4]} ({', '.join(d.get('transferors') or ['?'])}). A deed, will, "
                               f"or succession document between them is missing.", [prev["id"], d["id"]]))
        if d.get("executed_via_poa"):
            flags.append(_flag("AMBER", "Transfer executed through Power of Attorney (GPA)",
                               f"{d['date'][:4]} {DOC_LABELS[d['doc_type']].lower()} signed by GPA holder "
                               f"{d.get('poa_holder') or '(name not read)'}. Verify GPA is registered, unrevoked "
                               f"and the principal was alive on execution date (Suraj Lamp judgment compliance).", [d["id"]]))
    if not transfers:
        flags.append(_flag("RED", "No title deed found",
                           "None of the uploaded documents was read as a sale, gift, partition or conveyance deed.", []))
    else:
        span = today.year - _year(transfers[0]["date"])
        if span < 30:
            flags.append(_flag("AMBER", f"Title history covers only {span} years",
                               "Lenders and lawyers usually expect a 30-year flow of title. Ask for earlier parent documents.",
                               [transfers[0]["id"]]))

    ecs = by_type.get("ENCUMBRANCE_CERTIFICATE", [])
    if not ecs:
        flags.append(_flag("RED", "No Encumbrance Certificate",
                           "Without an EC / search report, mortgages, liens or later sales on the property cannot be ruled out.", []))
    else:
        years = []
        for e in ecs:
            p = e.get("ec_period") or {}
            if _year(p.get("from")) and _year(p.get("to")):
                years.append(_year(p["to"]) - _year(p["from"]))
        if years and max(years) < 30:
            flags.append(_flag("AMBER", f"EC covers only {max(years)} years",
                               "Obtain an EC / search report for at least 30 years.", [e["id"] for e in ecs]))

    has_noc = bool(by_type.get("LOAN_NOC"))
    for d in docs:
        for enc in d.get("encumbrances") or []:
            status = (enc.get("status") or "UNKNOWN").upper()
            if status == "RELEASED":
                continue
            what = f"{enc.get('nature') or 'Charge'} ({enc.get('date') or 'date ?'}, {enc.get('parties') or 'party ?'})"
            if has_noc:
                flags.append(_flag("AMBER", "Mortgage shown — Bank NOC uploaded",
                                   f"{what}. Bank NOC is present; confirm registered deed of release / redemption with sub-registrar.", [d["id"]]))
            else:
                flags.append(_flag("RED", "Active encumbrance / mortgage",
                                   f"{what} appears without a release deed or bank NOC.", [d["id"]]))

    records = [d for d in docs if d.get("doc_type") in RECORD_TYPES]
    if not records:
        flags.append(_flag("AMBER", "No revenue / mutation record",
                           "Upload Jamabandi, Fard, Intaqal, MCD Tax or mutation receipt to confirm owner in government records.", []))
    elif transfers:
        latest = transfers[-1].get("transferees") or []
        for r in records:
            owners = r.get("owner_in_record") or []
            if owners and not same_party(owners, latest):
                flags.append(_flag("RED", "Owner in government record differs from latest buyer",
                                   f"{DOC_LABELS[r['doc_type']]} shows {', '.join(owners)}; latest deed buyer is "
                                   f"{', '.join(latest)}. Mutation / Intaqal may be pending or title is disputed.",
                                   [r["id"], transfers[-1]["id"]]))

    for field, label in (("survey_no", "Khasra / Survey number"), ("flat_or_plot_no", "Flat / plot number"), ("khata_no", "Khata number")):
        seen = {}
        for d in docs:
            v = _norm_id((d.get("property") or {}).get(field))
            if v:
                seen.setdefault(v, []).append(d["id"])
        if len(seen) > 1:
            flags.append(_flag("RED", f"{label} does not match across documents",
                               "Values found: " + ", ".join(seen.keys()) + ". Confirm all papers refer to the same property.",
                               [i for ids in seen.values() for i in ids]))

    is_flat = any((d.get("property") or {}).get("flat_or_plot_no", "") and "flat" in
                  str((d.get("property") or {}).get("flat_or_plot_no", "")).lower() for d in docs)
    if is_flat and not by_type.get("OC_CC"):
        flags.append(_flag("INFO", "Occupancy / completion certificate not uploaded",
                           "For apartments, check MCD/DDA/DTCP OC/CC to rule out unauthorised construction.", []))

    for d in docs:
        for obs in d.get("observations") or []:
            if re.search(r"minor|court|litigation|dispute|stay|unregistered|attach|lis pendens|forg", obs, re.I):
                flags.append(_flag("AMBER", "AI observation needs review", obs, [d["id"]]))
        if (d.get("confidence") or 1) < 0.6:
            flags.append(_flag("INFO", "Low-confidence reading",
                               f"{d.get('filename')}: scan quality or handwriting made extraction uncertain. Verify fields manually.", [d["id"]]))

    score = max(0, 100 - sum(PENALTY[f["level"]] for f in flags))
    verdict = "LOW" if score >= 80 else "MEDIUM" if score >= 50 else "HIGH"
    order = {"RED": 0, "AMBER": 1, "INFO": 2}
    flags.sort(key=lambda f: order[f["level"]])

    def present(types):
        return [d["id"] for d in docs if d.get("doc_type") in types]

    checklist = [
        {"item": "Title deeds (chain of ownership)", "docs": present(TRANSFER_TYPES),
         "ok": bool(transfers) and all(c["link_ok"] for c in chain)},
        {"item": "Encumbrance certificate / 30-year search", "docs": present({"ENCUMBRANCE_CERTIFICATE"}),
         "ok": bool(ecs) and not any(f["title"].startswith("EC covers") for f in flags)},
        {"item": "Revenue / Jamabandi / Mutation record", "docs": present({"LAND_RECORD", "MUTATION"}), "ok": None},
        {"item": "Property tax receipt (MCD / MCG)", "docs": present({"PROPERTY_TAX"}), "ok": None},
        {"item": "OC / CC / Sanctioned building plan", "docs": present({"OC_CC", "BUILDING_APPROVAL", "RERA"}), "ok": None},
        {"item": "Bank Loan NOC / Release Deed", "docs": present({"LOAN_NOC"}), "ok": None},
    ]
    for c in checklist:
        if c["ok"] is None:
            c["ok"] = bool(c["docs"])

    prop = {}
    for d in reversed(transfers or docs):
        for k, v in (d.get("property") or {}).items():
            if v and not prop.get(k):
                prop[k] = v
    current_owner = (transfers[-1].get("transferees") if transfers else []) or []
    return {
        "chain": chain, "flags": flags, "score": score, "verdict": verdict, "checklist": checklist,
        "property": prop, "current_owner": current_owner,
        "counts": {k: sum(1 for f in flags if f["level"] == k) for k in ("RED", "AMBER", "INFO")}
    }


def _doc(i, doc_type, filename, **kw):
    base = {
        "id": f"s{i}", "doc_type": doc_type, "filename": filename, "language": "Hindi / English", "title": "",
        "date": None, "registration_no": None, "sub_registrar_office": None, "transferors": [],
        "transferees": [], "consideration": None, "property": {}, "executed_via_poa": False, "poa_holder": None,
        "encumbrances": [], "ec_period": None, "owner_in_record": [], "observations": [], "confidence": 0.95
    }
    base.update(kw)
    return base


DELHI_PROP = {
    "survey_no": "Khasra No. 87/12", "flat_or_plot_no": "Flat 702, Tower B",
    "village_or_locality": "Sector 9, Dwarka", "city_district": "New Delhi",
    "area": "1,050 sq ft carpet"
}
GGN_PROP = {
    "survey_no": "Khasra No. 45/2", "flat_or_plot_no": "Plot 18", "khata_no": "1187/45/2",
    "village_or_locality": "Sector 57, Wazirabad", "city_district": "Gurugram (Gurgaon)",
    "area": "30x40 ft (250 sq yd)"
}

SAMPLES = {
    "clean_delhi_flat": {
        "name": "Sample · 2BHK flat, Dwarka, New Delhi (clean title)", "state": "Delhi",
        "docs": [
            _doc(1, "CONVEYANCE_DEED", "1998_conveyance_land.pdf", language="Hindi", date="1998-03-14",
                 title="Conveyance of land to developer", registration_no="SR5/DEL/1840/1998",
                 sub_registrar_office="Sub-Registrar V, New Delhi District",
                 transferors=["Shri Dattatray Bhosale"], transferees=["M/s Sahyadri Constructions"],
                 consideration=1850000, property={**DELHI_PROP, "flat_or_plot_no": None}),
            _doc(2, "SALE_DEED", "2008_sale_deed_flat702.pdf", date="2008-07-22", title="Sale deed (builder flat)",
                 registration_no="SR5/DEL/6612/2008", sub_registrar_office="Sub-Registrar V, New Delhi District",
                 transferors=["M/s Sahyadri Constructions"], transferees=["Anil Deshmukh"],
                 consideration=2850000, property=DELHI_PROP),
            _doc(3, "SALE_DEED", "2016_sale_deed_resale.pdf", date="2016-11-03", title="Sale deed (resale)",
                 registration_no="SR5/DEL/9921/2016", sub_registrar_office="Sub-Registrar V, New Delhi District",
                 transferors=["Anil Deshmukh"], transferees=["Priya Nair"],
                 consideration=7400000, property=DELHI_PROP),
            _doc(4, "ENCUMBRANCE_CERTIFICATE", "EC_1998_2024.pdf", title="Search report 1998–2024",
                 ec_period={"from": "1998-01-01", "to": "2024-09-30"}, property=DELHI_PROP,
                 encumbrances=[{"date": "2016-11-03", "nature": "Equitable mortgage", "parties": "HDFC Bank",
                                "amount": 5000000, "status": "ACTIVE"}]),
            _doc(5, "LOAN_NOC", "HDFC_NOC_2023.pdf", date="2023-05-18", title="Loan closure NOC — HDFC Bank",
                 property=DELHI_PROP),
            _doc(6, "PROPERTY_TAX", "MCD_tax_2024.pdf", date="2024-04-10", title="MCD property tax receipt",
                 owner_in_record=["Priya Nair"], property=DELHI_PROP),
            _doc(7, "BUILDING_APPROVAL", "sanctioned_plan.pdf", date="2005-06-21",
                 title="Sanctioned building plan — MCD", property=DELHI_PROP),
            _doc(8, "OC_CC", "completion_certificate_2008.pdf", date="2008-02-11",
                 title="Completion certificate — MCD", property=DELHI_PROP),
        ],
    },
    "risky_gurgaon_plot": {
        "name": "Sample · residential plot, Sector 57, Gurgaon (risky title)", "state": "Haryana",
        "docs": [
            _doc(1, "SALE_DEED", "2001_sale_deed_hindi.pdf", language="Hindi", date="2001-06-09",
                 title="Sale deed", registration_no="GGM/WZB/3311/2001-02",
                 sub_registrar_office="Sub-Registrar, Wazirabad (Gurgaon)",
                 transferors=["Muniyappa"], transferees=["Venkatesh Gowda"],
                 consideration=240000, property=GGN_PROP, confidence=0.72),
            _doc(2, "SALE_DEED", "2015_sale_deed.pdf", date="2015-01-27", title="Sale deed",
                 registration_no="GGM/WZB/8820/2014-15", sub_registrar_office="Sub-Registrar, Wazirabad (Gurgaon)",
                 transferors=["Lakshmi Devi"], transferees=["Rahul Sharma"],
                 consideration=3600000, property=GGN_PROP,
                 observations=["Recital says vendor inherited from husband; no death certificate or succession document referenced in the deed."]),
            _doc(3, "SALE_DEED", "2022_sale_deed_gpa.pdf", date="2022-08-16", title="Sale deed (executed by GPA)",
                 registration_no="GGM/WZB/4410/2022-23", sub_registrar_office="Sub-Registrar, Wazirabad (Gurgaon)",
                 transferors=["Rahul Sharma"], transferees=["Kiran Rao"],
                 consideration=6900000, executed_via_poa=True, poa_holder="Suresh Babu (GPA holder)",
                 property={**GGN_PROP, "survey_no": "Khasra No. 45/3"}),
            _doc(4, "ENCUMBRANCE_CERTIFICATE", "EC_Tehsil_Gurgaon_2010_2024.pdf", title="EC — Tehsil Gurgaon",
                 ec_period={"from": "2010-04-01", "to": "2024-08-31"}, property=GGN_PROP,
                 encumbrances=[{"date": "2019-03-12", "nature": "Mortgage by deposit of title deeds",
                                "parties": "Canara Bank", "amount": 2500000, "status": "ACTIVE"}],
                 observations=["O.S. No. 2231/2020 (Civil Court, Gurgaon) noted against the khasra number."]),
            _doc(5, "LAND_RECORD", "Jamabandi_nakal.pdf", language="Hindi", title="Jamabandi / mutation nakal",
                 owner_in_record=["Rahul Sharma"], property=GGN_PROP),
        ],
    },
}

# ==============================================================================
# 2. GEMINI AI MULTIMODAL EXTRACTION
# ==============================================================================

# Updated model as recommended by the API error
MODEL = "gemini-3.8-flash"
MIMES = {
    ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".webp": "image/webp"
}
SYSTEM = (
    "You are an assistant to Indian real-estate lawyers. You read property documents (sale deeds, EC, 7/12, "
    "RTC, Khata, Jamabandi, mutation, tax receipts, OC/CC, RERA, POA, bank NOC) in English, Hindi, Marathi, "
    "Kannada, Tamil, Telugu, Gujarati or Bengali, including scanned or handwritten pages. Extract facts only "
    "from the document; never guess. Translate names to English (Latin script). Output strict JSON only."
)
PROMPT = """Extract this document into JSON with exactly these keys:
{"doc_type": one of SALE_DEED|GIFT_DEED|PARTITION_DEED|RELEASE_DEED|WILL|SETTLEMENT_DEED|CONVEYANCE_DEED|POA|ENCUMBRANCE_CERTIFICATE|LAND_RECORD|MUTATION|PROPERTY_TAX|OC_CC|BUILDING_APPROVAL|RERA|LOAN_NOC|OTHER,
 "language": main language, "title": short English description,
 "date": YYYY-MM-DD or null, "registration_no": string or null, "sub_registrar_office": string or null,
 "transferors": [seller/donor names], "transferees": [buyer/donee names], "consideration": number or null,
 "property": {"survey_no", "flat_or_plot_no", "khata_no", "village_or_locality", "city_district", "area"} (null when absent; include "Flat"/"Plot" in flat_or_plot_no),
 "executed_via_poa": true/false, "poa_holder": name or null,
 "encumbrances": [{"date", "nature", "parties", "amount", "status": "ACTIVE"|"RELEASED"|"UNKNOWN"}] (charges only, not sales),
 "ec_period": {"from": YYYY-MM-DD, "to": YYYY-MM-DD} or null,
 "owner_in_record": [owner names in a land/tax/mutation record],
 "observations": [notes a lawyer must check: minors, court cases, missing recitals, unregistered, stamp issues],
 "confidence": 0.0-1.0}
Mask Aadhaar and PAN numbers. Return JSON only."""
COLORS = {"RED": "🔴", "AMBER": "🟠", "INFO": "⚪"}


def extract(data, mime):
    client = genai.Client(api_key="AQ.Ab8RN6J3AyAr8Lp65os25Idp3ZTyImrlhpne4knz0dWvdxYAuw")
    resp = client.models.generate_content(
        model=MODEL,
        contents=[types.Part.from_bytes(data=data, mime_type=mime), PROMPT],
        config=types.GenerateContentConfig(system_instruction=SYSTEM, response_mime_type="application/json"),
    )
    out = json.loads(re.search(r"\{.*\}", resp.text, re.S).group(0))
    if out.get("doc_type") not in DOC_LABELS:
        out["doc_type"] = "OTHER"
    return out


# ==============================================================================
# 3. STREAMLIT INTERACTIVE UI & SCORECARD
# ==============================================================================

def show_report(name, docs):
    r = analyse(docs)
    verdict = {"LOW": "🟢 Low risk", "MEDIUM": "🟠 Medium risk", "HIGH": "🔴 High risk"}[r["verdict"]]
    st.header(name)
    p = r["property"]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Title Health Score", f"{r['score']}/100", verdict, delta_color="off")
    c2.metric("Critical", r["counts"]["RED"])
    c3.metric("Review", r["counts"]["AMBER"])
    c4.metric("Notes", r["counts"]["INFO"])
    st.write(f"**Property:** {' · '.join(filter(None, [p.get('flat_or_plot_no'), p.get('survey_no')])) or '—'}  \n"
             f"**Location:** {', '.join(filter(None, [p.get('village_or_locality'), p.get('city_district')])) or '—'}  \n"
             f"**Current owner (latest deed):** {', '.join(r['current_owner']) or '—'}")

    left, right = st.columns([1.2, 1])
    with left:
        st.subheader("Red flags & checks")
        if not r["flags"]:
            st.success("No issues detected in the uploaded documents.")
        for f in r["flags"]:
            st.markdown(f"{COLORS[f['level']]} **{f['title']}**  \n{f['detail']}")
    with right:
        st.subheader("Chain of title")
        for c in r["chain"]:
            if not c["link_ok"]:
                st.error("Gap — previous buyer is not this seller")
            st.markdown(f"**{c['date']}** · {DOC_LABELS[c['doc_type']]}{' · via GPA' if c['via_poa'] else ''}  \n"
                        f"{', '.join(c['from'])} → **{', '.join(c['to'])}**")
        st.subheader("Document checklist")
        for c in r["checklist"]:
            st.write(("✅ " if c["ok"] else "❌ ") + c["item"])

    with st.expander("Extracted data (lawyer view)"):
        for d in docs:
            st.markdown(f"**{DOC_LABELS[d['doc_type']]}** · {d.get('filename')}")
            st.json(d, expanded=False)

    st.subheader("Your feedback")
    with st.form(f"fb_{name}"):
        rating = st.slider("How useful is this report?", 1, 5, 4)
        accuracy = st.radio("Were the facts and flags correct?", ["Accurate", "Partly correct", "Mostly wrong"],
                            horizontal=True)
        role = st.selectbox("You are a…", ["Lawyer", "Bank / NBFC", "Broker", "Builder", "Buyer"])
        pay = st.selectbox("Would you pay per property?", ["No", "Under ₹1,000", "₹1,000–2,500", "Above ₹2,500"])
        comment = st.text_area("What was missing or wrong?")
        if st.form_submit_button("Submit feedback"):
            with open("feedback.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"case": name, "score": r["score"], "rating": rating, "accuracy": accuracy,
                                     "role": role, "would_pay": pay, "comment": comment}) + "\n")
            st.success("Thank you — feedback saved.")


st.set_page_config(page_title="Property Title AI", page_icon="🏠", layout="wide")
st.title("🏠 Property Title AI — Prototype")
st.warning("Preliminary review only. AI-read documents can be wrong. Not a legal title opinion — always have an "
           "advocate verify originals and government records before paying.")

tab_ai, tab_sample, tab_fb = st.tabs(["Analyse documents", "Sample cases (instant)", "Feedback board"])

with tab_ai:
    name = st.text_input("Case name", "My property case")

    files = st.file_uploader(
        "Upload sale deeds, EC, Jamabandi / 7/12 / RTC / Khata, tax receipt, OC, NOC (PDF/JPG/PNG)",
        type=["pdf", "png", "jpg", "jpeg", "webp"], accept_multiple_files=True
    )
    
    if files and st.button("Generate title scorecard", type="primary"):
        docs, bar = [], st.progress(0.0, "Reading documents…")
        for i, f in enumerate(files[:10]):
            try:
                d = extract(f.getvalue(), MIMES[Path(f.name).suffix.lower()])
                docs.append({**mask_obj(d), "id": f"d{i + 1}", "filename": f.name})
            except Exception as e:
                st.error(f"{f.name}: could not be read ({e})")
            bar.progress((i + 1) / len(files), f"{i + 1} of {len(files)} done")
        st.session_state["ai_case"] = (name, docs)

    if st.session_state.get("ai_case") and st.session_state["ai_case"][1]:
        show_report(*st.session_state["ai_case"])

with tab_sample:
    key = st.radio("Pick a fictional sample", list(SAMPLES), format_func=lambda k: SAMPLES[k]["name"])
    show_report(SAMPLES[key]["name"], SAMPLES[key]["docs"])

with tab_fb:
    if Path("feedback.jsonl").exists():
        rows = [json.loads(line) for line in Path("feedback.jsonl").read_text(encoding="utf-8").splitlines()]
        st.metric("Total feedback responses", len(rows))
        st.dataframe(rows, use_container_width=True)
    else:
        st.info("No feedback yet.")