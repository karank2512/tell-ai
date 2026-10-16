"""CRM write-back: tell's score and its receipt on the customer's own CRM record.

The first destination is HubSpot (:mod:`tell.crm.hubspot`, ADR 0037): a company is
upserted by domain, five ``tell_*`` company properties carry the score, and a note on the
company's timeline carries the receipt (one dated, linked line per contribution). Only
company-level data ever leaves tell; no contact is read or written.
"""
