# Use one durable research worker

Research runs and chat replies are processed by one long-running worker with separate bounded pools and database-enforced leases. Polling SQLite avoids path-triggered singleton limits, keeps three long research runs from blocking chat, and preserves correctness if more than one worker process is started accidentally.
