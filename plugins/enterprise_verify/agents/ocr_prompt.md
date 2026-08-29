<image>
<|grounding|>Extract all text from this business license image.
Return ONLY valid JSON with these exact fields:
{
  "company_name": "Company Name",
  "reg_num": "Unified Social Credit Code (18 digits)",
  "legal_person": "Legal Representative",
  "address": "Registered Address",
  "registered_capital": "Registered Capital",
  "business_scope": "Business Scope"
}
Fill empty string for any field not found. MUST output pure JSON.
