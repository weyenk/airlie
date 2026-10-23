"""The supervisor: a second model (Muse) that reads a role's work and a failed check's output, and says what
went wrong and what the role should do next, before a layer blocks. Every intervention is recorded in an
append-only ledger so a person can judge whether it helped."""
