---
name: pl-read-mail
description: Read an inbound email thread for a pl card and output fixed fields as a single JSON object.
pl-builtin-version: 1
---

# pl-read-mail: extract structured fields from inbound email

Read the email thread for the given card and extract the sender's details and request.

## Output format
Output exactly ONE JSON object with no markdown formatting and no extra text around it:
{
  "name": "sender's full name",
  "company": "sender's company or organization",
  "ask": "what they are asking for or need",
  "summary": "a short summary of the message"
}
