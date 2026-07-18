# Durably stage research evidence

Completed research steps are persisted behind the active run claim before a snapshot is sealed. This adds intermediate SQLite writes, but preserves collected evidence across crashes and cancellation while keeping only sealed cumulative evidence views available for synthesis and citation.
