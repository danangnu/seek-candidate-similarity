import json
from bs4 import BeautifulSoup
import ollama


def extract_candidate_profile(html_content: str) -> dict:
    soup = BeautifulSoup(html_content, "html.parser")

    # 1. Safely remove ONLY the "Similar candidates" container
    for header in soup.find_all(
        lambda t: t.name in ["h3", "h4", "span"]
        and t.string
        and "Similar candidates" in t.string
    ):
        parent_container = header.find_parent("div")
        if parent_container:
            parent_container.decompose()

    # 2. Extract Candidate Name (h2 tag)
    name_elem = soup.find("h2")
    candidate_name = name_elem.get_text(strip=True) if name_elem else "Unknown"

    # 3. Extract Work History entries
    work_history = []
    # Primary role header
    role_header = soup.find(attrs={"data-testid": "work-history"})
    if role_header:
        work_history.append(role_header.get_text(separator=" ", strip=True))

    # Historical career roles
    for heading in soup.find_all(attrs={"data-testid": "subHeading"}):
        role_text = heading.get_text(strip=True)
        date_elem = heading.find_next_sibling(
            attrs={"data-testid": "subHeadingSecondary"}
        )
        date_text = date_elem.get_text(strip=True) if date_elem else ""
        work_history.append(f"{role_text} ({date_text})".strip())

    # 4. Extract Personal Summary
    summary = ""
    summary_heading = soup.find(
        lambda t: t.name in ["h4", "h3"]
        and t.string
        and "Personal summary" in t.string
    )
    if summary_heading:
        summary_container = summary_heading.find_next_sibling("span")
        if summary_container:
            summary = summary_container.get_text(separator=" ", strip=True)

    # 5. Extract Licences
    licences = []
    for h4 in soup.find_all(
        lambda t: t.name == "h4"
        and t.string
        and "Licences & certifications" in t.string
    ):
        parent = h4.find_parent("div")
        if parent:
            for item in parent.find_all("span"):
                txt = item.get_text(strip=True)
                if (
                    "Licence" in txt or "Card" in txt
                ) and txt not in licences:
                    licences.append(txt)

    return {
        "candidate_name": candidate_name,
        "summary": summary,
        "work_history": work_history,
        "licences": licences,
    }


def compare_profiles_with_ollama(
    profile_a: dict, profile_b: dict, model: str = "llama3.1"
) -> dict:
    prompt = f"""
You are an entity resolution assistant.
Compare Candidate Profile A and Candidate Profile B. Determine if both profiles belong to the exact same human being.

Profile A:
{json.dumps(profile_a, indent=2)}

Profile B:
{json.dumps(profile_b, indent=2)}

Respond ONLY with valid JSON matching this schema:
{{
  "is_same_person": true or false,
  "confidence": float between 0.0 and 1.0,
  "reason": "Brief 1-2 sentence explanation"
}}
"""
    response = ollama.chat(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        format="json",
        options={"temperature": 0.0},
    )
    return json.loads(response["message"]["content"])


# --- Run script ---
with open("seekid.txt", "r", encoding="utf-8") as f:
    data_1 = extract_candidate_profile(f.read())

with open("uuid.txt", "r", encoding="utf-8") as f:
    data_2 = extract_candidate_profile(f.read())

print("Extracted Data 1:\n", json.dumps(data_1, indent=2))
print("\nExtracted Data 2:\n", json.dumps(data_2, indent=2))

result = compare_profiles_with_ollama(data_1, data_2)
print("\nOllama Verdict:\n", json.dumps(result, indent=2))