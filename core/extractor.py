import json, re, time
from langchain_groq import ChatGroq

FIELDS = [
    "Name", "Gender", "DOB", "Age", "TOB", "POB",
    "Maritalstatus", "Education", "Occupation", "Annualincome",
    "Religion", "Caste", "Subcaste", "Gothram", "Language",
    "Star", "Moonsign", "Height", "Weight", "BloodGroup",
    "Complexion", "Diet", "Smoke", "Drink",
    "Address", "City", "State", "Country", "Pincode", "Mobile",
    "Fathername", "Mothersname", "Fathersoccupation", "Mothersoccupation",
    "noofbrothers", "noofsisters", "FamilyType", "FamilyStatus",
    "PartnerExpectations", "Hobbies"
]

PROMPT = """You are a matrimonial profile data extraction agent.
Text may be Marathi, Hindi, or English. Extract all info, translate to English.
Return ONLY a valid JSON object. Use null for missing fields.

Keys: {fields}

Rules:
- DOB: YYYY-MM-DD
- Height: integer cm
- Age: string e.g. "28"
- Name: candidate full name only
- Return ONLY raw JSON, no markdown

Text:
\"\"\"{text}\"\"\"

JSON:"""

# Models ranked by token efficiency (use smaller when rate limited)
FALLBACK_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "gemma2-9b-it",
]

def build_llm(config, model=None):
    m = model or config["model"]
    return ChatGroq(model=m, api_key=config["api_key"], temperature=0)

def extract_profile(llm, text, max_chars=5000, api_key=None, retry_delay=5, max_retries=3):
    """Extract with automatic retry + model fallback on rate limit."""
    prompt = PROMPT.format(fields=json.dumps(FIELDS), text=text[:max_chars])
    
    current_llm = llm
    model_idx = 0

    for attempt in range(max_retries):
        try:
            response = current_llm.invoke(prompt)
            raw = re.sub(r'```json|```', '', response.content).strip()
            
            # Fix common JSON issues: multiple objects, trailing commas
            raw = re.sub(r',\s*}', '}', raw)
            raw = re.sub(r',\s*]', ']', raw)
            
            match = re.search(r'\{.*?\}(?=\s*$|\s*\{)', raw, re.DOTALL)
            if not match:
                match = re.search(r'\{.*\}', raw, re.DOTALL)
            if not match:
                # Maybe LLM returned a list — take first item
                arr_match = re.search(r'\[.*\]', raw, re.DOTALL)
                if arr_match:
                    arr = json.loads(arr_match.group())
                    if isinstance(arr, list) and arr and isinstance(arr[0], dict):
                        return arr[0], None
                return None, "No JSON in response"
            
            parsed = json.loads(match.group())
            # Handle if it's a list
            if isinstance(parsed, list):
                parsed = parsed[0] if parsed and isinstance(parsed[0], dict) else {}
            # Skip if values look like garbled/encoded text (non-latin garbage)
            name = parsed.get("Name", "") or ""
            if name and all(ord(c) > 127 for c in name.replace(" ","")[:6]):
                parsed["Name"] = None  # clear garbled name, keep other fields
            return parsed, None

        except json.JSONDecodeError as e:
            # Try to extract just the first valid JSON object
            try:
                decoder = json.JSONDecoder()
                raw_clean = re.sub(r'```json|```', '', response.content).strip()
                obj, _ = decoder.raw_decode(raw_clean)
                return obj, None
            except Exception:
                return None, f"JSON parse error: {e}"

        except Exception as e:
            err_str = str(e)
            
            # Rate limit hit
            if '429' in err_str or 'rate_limit_exceeded' in err_str:
                # Try next smaller model
                model_idx += 1
                if model_idx < len(FALLBACK_MODELS) and api_key:
                    next_model = FALLBACK_MODELS[model_idx]
                    current_llm = ChatGroq(model=next_model, api_key=api_key, temperature=0)
                    wait = retry_delay * (attempt + 1)
                    return None, f"RATE_LIMIT|{next_model}|{wait}"
                else:
                    # Extract wait time from error message
                    wait_match = re.search(r'Please try again in (\d+)m', err_str)
                    wait_mins = int(wait_match.group(1)) if wait_match else 30
                    return None, f"RATE_LIMIT_HARD|{wait_mins}"
            
            return None, str(e)
    
    return None, "Max retries exceeded"



def is_valid_profile(profile):
    if not isinstance(profile, dict):
        return False
    name   = str(profile.get("Name")   or "")
    mobile = str(profile.get("Mobile") or "")
    dob    = str(profile.get("DOB")    or "")
    age    = str(profile.get("Age")    or "")
    # Reject if name is all garbled non-latin chars
    if name and all(ord(c) > 127 for c in name.replace(" ","")[:8]):
        name = ""
    return bool(name or mobile or dob or age)