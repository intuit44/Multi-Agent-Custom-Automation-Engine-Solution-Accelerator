# Infra Scripts (Azure AI Foundry)

Este directorio contiene scripts para gestionar agentes, tokens, y cuotas en Azure AI Foundry. Los scripts están diseñados para automatizar tareas comunes relacionadas con la administración de agentes y la validación de recursos en Azure.

## Scripts principales

### `generar_token_azure.ps1`
- **Descripción:** Genera un token de acceso (`access_token`) para interactuar con los servicios de Azure AI Foundry.
- **Salida:** Guarda el token como un archivo JSON (`generated_token.json`) en el mismo directorio.
- **Uso:**
  ```powershell
  .\generar_token_azure.ps1