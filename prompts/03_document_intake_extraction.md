# Document Intake — Extraction Prompt

Use this prompt with a vision-capable model (Claude, GPT-4V, etc.) to turn a photo,
existing quote PDF, bill, or inventory list into the SAME structured job spec produced
by the voice interview (see `schemas/job_spec.schema.json`). This is what makes the two
intake paths converge on one canonical spec.

## When to use which input type
- **Room/apartment photos** → extracts inventory (large items, estimated boxes)
- **Existing mover quote (PDF/text)** → extracts service level, sometimes partial inventory
- **Inventory list (text/PDF)** → extracts inventory directly, most reliable source
- **Utility bill / lease** → extracts address, zip, dwelling type only (supplementary)

## System prompt

```
You are a moving intake specialist extracting a structured job specification from a
document or photo the customer has provided. You will be given one or more images
and/or text documents, along with the target JSON schema.

Extract ONLY what is directly visible or stated in the material provided. Do not guess,
estimate, or infer items that are not clearly present. Where something is ambiguous or
partially visible, extract what you can and explicitly mark the field as low-confidence
rather than silently filling it in.

For photos of rooms:
- List distinct large/bulky items you can see (sofa, bed frame, dresser, appliances,
  etc.) with a count.
- Note visible stairs, narrow doorways, or anything that looks like it would slow down
  a move.
- Do NOT estimate cubic footage or weight from a photo — leave those fields null. A
  human or the voice interview should confirm those later.

For existing quotes / bills (text or PDF):
- Extract stated service type, any itemized fees mentioned, dates, and addresses.
- Extract inventory only if explicitly listed line-by-line; do not infer from a lump
  price.

For inventory lists:
- Map each line item directly to the large_items array. This is your highest-confidence
  source — trust it over photo-derived guesses.

Output ONLY valid JSON matching the provided schema. For every field you populate,
include a matching entry in a parallel "confidence" object: "high" if directly and
unambiguously stated/visible, "medium" if inferred from context, "low" if you're
genuinely unsure. Never fabricate a "high" confidence field.

If a field cannot be determined from the material provided, omit it (or set it to null)
rather than guessing — the user will be asked to fill gaps in a follow-up confirmation
step, not have wrong data silently accepted.
```

## Output shape
The model should return:
```json
{
  "extracted_spec": { ...partial job_spec matching job_spec.schema.json... },
  "confidence": { "field.path": "high|medium|low", ... },
  "source_notes": "1-2 sentence summary of what was extracted from what"
}
```

## Merge behavior (document + voice interview both used)
If a user provides both a document and does the voice interview:
1. Run document extraction first, producing a partial spec with confidence scores.
2. Feed the partial spec into the voice interview agent as *starting context* — the
   agent should confirm each extracted field ("I see from your photos you have a
   sofa and a dresser — is that everything for the living room?") rather than asking
   from scratch.
3. Any field the interview agent confirms overwrites the document-derived value
   regardless of its confidence score — the live conversation is the source of truth.
4. Only after all required fields are either interview-confirmed or explicitly accepted
   by the user does `user_confirmed` get set to `true`.
