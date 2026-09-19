# Wilcom warehouse helper setup

The helper receives an immediate request from an embroidery tablet, opens the
exact `G:\My Drive\Orders\<order>\<order>.EMB` file, selects the configured
Wilcom EmbroideryConnect device, and reports success or failure to the tablet.

## Render

1. Generate a long random token.
2. Add it to the backend's Render environment as `WILCOM_AGENT_TOKEN`.
3. Redeploy the backend.

The same token goes in the warehouse computer's local config. Never commit the
real token.

## Main warehouse Windows computer

1. Copy these files into one folder on the Wilcom computer:
   - `warehouse_wilcom_agent.py`
   - `warehouse-agent-requirements.txt`
   - `wilcom_agent_config.example.json`
   - `install_wilcom_agent.ps1`
2. Run PowerShell in that folder:

   `powershell -ExecutionPolicy Bypass -File .\install_wilcom_agent.ps1`

3. Edit the generated `wilcom_agent_config.json`.
4. Replace `agentToken` with the Render token.
5. Replace all four `REPLACE_WITH_WILCOM_DEVICE_NAME_*` values with the exact
   names shown in Wilcom's **Send to Device/Machine** dialog.
6. Sign out and back in, or run the Startup launcher once.

The agent must run in the signed-in, unlocked Windows desktop session. Keep
EmbroideryHub running. UI automation cannot operate while Windows is locked.

## Placeholder behavior

Until a machine's placeholder is replaced, that machine's tablet displays
**Machine mapping needed** and the backend refuses to queue a send. This avoids
accidentally sending an order to the wrong physical machine.

## Tablet designation

On each tablet's machine page, tap **Set this tablet's machine**, choose Machine
1–4, and confirm. The selection is stored only on that tablet.
