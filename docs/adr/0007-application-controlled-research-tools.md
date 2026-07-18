# Keep research tool execution application-controlled

Research AI may propose and revise a Research Plan, including automatically adding existing credentialless connectors, but it never invokes connectors or general tools directly. The application validates every connector type and configuration before execution, and any AI call that reads externally sourced evidence remains tool-free so prompt injection cannot turn source content into executable instructions.
