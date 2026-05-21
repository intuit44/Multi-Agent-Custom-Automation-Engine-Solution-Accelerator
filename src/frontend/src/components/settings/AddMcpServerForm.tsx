/**
 * AddMcpServerForm - Formulario para registrar un nuevo servidor MCP en el catálogo
 */

import React, { useState } from 'react';
import {
  Button,
  Input,
  Textarea,
  Select,
  Field,
  Spinner,
  Badge,
  Tooltip,
  MessageBar,
  MessageBarBody,
} from '@fluentui/react-components';
import {
  Add24Regular,
  Dismiss24Regular,
  Info24Regular,
  CheckmarkCircle24Filled,
  Globe24Regular,
} from '@fluentui/react-icons';
import { apiService } from '../../api/apiService';
import { TokenInput } from './TokenInput';

// ─── Tipos ────────────────────────────────────────────────────────────────────

interface AddMcpServerFormProps {
  onSuccess: () => void;
  onCancel: () => void;
}

interface FormState {
  server_name: string;
  display_name: string;
  endpoint: string;
  transport: 'streamable-http' | 'stdio';
  auth_type: 'none' | 'oauth2' | 'api_key' | 'bearer_token';
  description: string;
  icon_url: string;
  auth_fields: string[];
  oauth_scopes: string[];
  capabilities: string[];
  allowed_agents: string[];
  // Stdio-specific fields
  command: string;
  args: string[];
  env: Record<string, string>;
}

interface FormErrors {
  server_name?: string;
  display_name?: string;
  endpoint?: string;
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

const INITIAL_STATE: FormState = {
  server_name: '',
  display_name: '',
  endpoint: '',
  transport: 'streamable-http',
  auth_type: 'none',
  description: '',
  icon_url: '',
  auth_fields: [],
  oauth_scopes: [],
  capabilities: ['tools'],
  allowed_agents: [],
  command: '',
  args: [],
  env: {},
};

const CAPABILITY_OPTIONS = ['tools', 'resources', 'prompts'];

function toSlug(value: string): string {
  return value
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-|-$/g, '');
}

function validateUrl(url: string): boolean {
  try {
    new URL(url);
    return true;
  } catch {
    return false;
  }
}

function validateSlug(slug: string): boolean {
  return /^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$/.test(slug) || /^[a-z0-9]$/.test(slug);
}

// ─── Componente principal ─────────────────────────────────────────────────────

export const AddMcpServerForm: React.FC<AddMcpServerFormProps> = ({ onSuccess, onCancel }) => {
  const [form, setForm] = useState<FormState>(INITIAL_STATE);
  const [errors, setErrors] = useState<FormErrors>({});
  const [submitting, setSubmitting] = useState(false);
  const [serverError, setServerError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);
  const [slugManual, setSlugManual] = useState(false);

  // ── Helpers de edición ──────────────────────────────────────────────────────

  const set = (field: keyof FormState, value: any) => {
    setForm((prev) => ({ ...prev, [field]: value }));
    if (field in errors) setErrors((prev) => ({ ...prev, [field]: undefined }));
  };

  const handleDisplayNameChange = (value: string) => {
    set('display_name', value);
    if (!slugManual) {
      set('server_name', toSlug(value));
    }
  };

  const toggleCapability = (cap: string) => {
    set(
      'capabilities',
      form.capabilities.includes(cap)
        ? form.capabilities.filter((c) => c !== cap)
        : [...form.capabilities, cap]
    );
  };

  // ── Validación ──────────────────────────────────────────────────────────────

  const validate = (): boolean => {
    const newErrors: FormErrors = {};

    if (!form.display_name.trim()) {
      newErrors.display_name = 'El nombre es obligatorio';
    }

    if (!form.server_name.trim()) {
      newErrors.server_name = 'El identificador es obligatorio';
    } else if (!validateSlug(form.server_name)) {
      newErrors.server_name = 'Solo letras minúsculas, números y guiones (ej: github-mcp)';
    }

    // Validar endpoint solo para streamable-http
    if (form.transport === 'streamable-http') {
      if (!form.endpoint.trim()) {
        newErrors.endpoint = 'El endpoint es obligatorio';
      } else if (!validateUrl(form.endpoint)) {
        newErrors.endpoint = 'URL inválida (debe incluir http:// o https://)';
      }
    }

    // Validar command solo para stdio
    if (form.transport === 'stdio' && !form.command.trim()) {
      newErrors.server_name = 'El comando es obligatorio para servidores stdio';
    }

    setErrors(newErrors);
    return Object.keys(newErrors).length === 0;
  };

  // ── Submit ──────────────────────────────────────────────────────────────────

  const handleSubmit = async () => {
    if (!validate()) return;

    setSubmitting(true);
    setServerError(null);

    try {
      const payload = {
        server_name: form.server_name,
        display_name: form.display_name,
        endpoint: form.endpoint,
        transport: form.transport,
        auth_type: form.auth_type,
        description: form.description || '',
        icon_url: form.icon_url || null,
        auth_fields: form.auth_fields,
        oauth_scopes: form.oauth_scopes,
        capabilities: form.capabilities,
        allowed_agents: form.allowed_agents,
        enabled: true,
        // Stdio-specific
        ...(form.transport === 'stdio' && {
          command: form.command,
          args: form.args,
          env: form.env,
        }),
      };

      await apiService.registerMcpServer(payload);
      setSuccess(true);

      setTimeout(() => {
        onSuccess();
      }, 800);
    } catch (err: any) {
      const msg = err?.response?.data?.detail || err?.message || 'Error al registrar el servidor';
      setServerError(msg);
    } finally {
      setSubmitting(false);
    }
  };

  // ── Render ──────────────────────────────────────────────────────────────────

  if (success) {
    return (
      <div
        style={{
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          padding: 48,
          gap: 16,
        }}
      >
        <CheckmarkCircle24Filled
          style={{ width: 48, height: 48, color: 'var(--colorBrandForeground1)' }}
        />
        <div style={{ fontWeight: 600, fontSize: 16 }}>Servidor registrado correctamente</div>
        <div style={{ color: 'var(--colorNeutralForeground3)', fontSize: 13 }}>
          Ahora puedes conectarte desde la lista de aplicaciones.
        </div>
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 0 }}>
      {/* Header */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          marginBottom: 20,
        }}
      >
        <div>
          <div style={{ fontWeight: 700, fontSize: 16 }}>Agregar servidor MCP</div>
          <div style={{ color: 'var(--colorNeutralForeground3)', fontSize: 12, marginTop: 2 }}>
            Registra un endpoint MCP remoto para que los agentes puedan usarlo.
          </div>
        </div>
        <Button appearance="subtle" icon={<Dismiss24Regular />} onClick={onCancel} />
      </div>

      {/* Error del servidor */}
      {serverError && (
        <MessageBar intent="error" style={{ marginBottom: 16 }}>
          <MessageBarBody>{serverError}</MessageBarBody>
        </MessageBar>
      )}

      {/* Campos básicos */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
        {/* Nombre visible */}
        <Field
          label="Nombre"
          required
          validationState={errors.display_name ? 'error' : 'none'}
          validationMessage={errors.display_name}
          hint="Nombre legible que verá el usuario (ej: GitHub Copilot MCP)"
        >
          <Input
            value={form.display_name}
            onChange={(_, d) => handleDisplayNameChange(d.value)}
            placeholder="GitHub Copilot MCP"
            disabled={submitting}
          />
        </Field>

        {/* Identificador (slug) */}
        <Field
          label={
            <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
              Identificador (server_name)
              <Tooltip
                content="ID único usado internamente. Solo minúsculas, números y guiones."
                relationship="description"
              >
                <Info24Regular
                  style={{
                    width: 14,
                    cursor: 'help',
                    color: 'var(--colorNeutralForeground3)',
                  }}
                />
              </Tooltip>
            </span>
          }
          required
          validationState={errors.server_name ? 'error' : 'none'}
          validationMessage={errors.server_name}
        >
          <Input
            value={form.server_name}
            onChange={(_, d) => {
              setSlugManual(true);
              set('server_name', d.value);
            }}
            placeholder="github-copilot-mcp"
            disabled={submitting}
            contentBefore={
              <span style={{ color: 'var(--colorNeutralForeground3)', fontSize: 12 }}>#</span>
            }
          />
        </Field>

        {/* Endpoint (solo para streamable-http) */}
        {form.transport === 'streamable-http' && (
          <Field
            label="Endpoint URL"
            required
            validationState={errors.endpoint ? 'error' : 'none'}
            validationMessage={errors.endpoint}
            hint="URL del servidor MCP (ej: https://api.githubcopilot.com/mcp)"
          >
            <Input
              value={form.endpoint}
              onChange={(_, d) => set('endpoint', d.value)}
              placeholder="https://api.githubcopilot.com/mcp"
              disabled={submitting}
              contentBefore={
                <Globe24Regular style={{ width: 16, color: 'var(--colorNeutralForeground3)' }} />
              }
            />
          </Field>
        )}

        {/* Command (solo para stdio) */}
        {form.transport === 'stdio' && (
          <>
            <Field
              label="Command"
              required
              hint="Comando ejecutable (ej: npx, python, node)"
            >
              <Input
                value={form.command}
                onChange={(_, d) => set('command', d.value)}
                placeholder="npx"
                disabled={submitting}
              />
            </Field>

            <Field label="Arguments" hint="Argumentos del comando (uno por línea)">
              <TokenInput
                values={form.args}
                onChange={(vals) => set('args', vals)}
                placeholder="-y @modelcontextprotocol/server-github"
                disabled={submitting}
              />
            </Field>

            <Field
              label="Environment Variables"
              hint="Variables de entorno en formato KEY=value (una por línea)"
            >
              <Textarea
                value={Object.entries(form.env)
                  .map(([k, v]) => `${k}=${v}`)
                  .join('\n')}
                onChange={(_, d) => {
                  const lines = d.value.split('\n').filter((l) => l.trim());
                  const envObj: Record<string, string> = {};
                  lines.forEach((line) => {
                    const [key, ...valueParts] = line.split('=');
                    if (key) envObj[key.trim()] = valueParts.join('=').trim();
                  });
                  set('env', envObj);
                }}
                placeholder="GITHUB_TOKEN=ghp_xxxxxxxxxxxx"
                disabled={submitting}
                rows={3}
                resize="vertical"
              />
            </Field>
          </>
        )}

        {/* Descripción */}
        <Field label="Descripción" hint="Breve descripción del servidor (opcional)">
          <Textarea
            value={form.description}
            onChange={(_, d) => set('description', d.value)}
            placeholder="Gestiona issues, PRs y repositorios de GitHub"
            disabled={submitting}
            rows={2}
            resize="vertical"
          />
        </Field>

        {/* Configuración técnica */}
        <div
          style={{
            borderTop: '1px solid var(--colorNeutralStroke2)',
            paddingTop: 14,
            marginTop: 4,
          }}
        >
          <div
            style={{
              fontWeight: 600,
              fontSize: 13,
              marginBottom: 12,
              color: 'var(--colorNeutralForeground2)',
            }}
          >
            Configuración técnica
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
            {/* Transport */}
            <Field label="Transporte">
              <Select
                value={form.transport}
                onChange={(_, d) => {
                  const newTransport = d.value as 'streamable-http' | 'stdio';
                  set('transport', newTransport);
                  // Reset transport-specific fields
                  if (newTransport === 'stdio') {
                    set('endpoint', ''); // stdio no usa endpoint
                  } else {
                    set('command', '');
                    set('args', []);
                    set('env', {});
                  }
                }}
                disabled={submitting}
              >
                <option value="streamable-http">streamable-http</option>
                <option value="stdio">stdio (local)</option>
              </Select>
            </Field>

            {/* Auth type */}
            <Field label="Autenticación">
              <Select
                value={form.auth_type}
                onChange={(_, d) => {
                  set('auth_type', d.value as any);
                  if (d.value === 'none') {
                    set('auth_fields', []);
                    set('oauth_scopes', []);
                  }
                }}
                disabled={submitting}
              >
                <option value="none">Sin autenticación</option>
                <option value="oauth2">OAuth 2.0</option>
                <option value="api_key">API Key</option>
                <option value="bearer_token">Bearer Token</option>
              </Select>
            </Field>
          </div>
        </div>

        {/* Auth fields — solo si no es "none" */}
        {form.auth_type !== 'none' && (
          <div
            style={{
              display: 'flex',
              flexDirection: 'column',
              gap: 10,
              paddingLeft: 12,
              borderLeft: '2px solid var(--colorBrandBackground)',
            }}
          >
            <Field
              label="Campos de autenticación"
              hint={
                form.auth_type === 'oauth2'
                  ? 'Campos del token OAuth que se guardarán (ej: access_token)'
                  : 'Nombre del campo donde va la API key (ej: api_key)'
              }
            >
              <TokenInput
                values={form.auth_fields}
                onChange={(vals) => set('auth_fields', vals)}
                placeholder="access_token"
                disabled={submitting}
              />
            </Field>

            {form.auth_type === 'oauth2' && (
              <Field
                label="OAuth Scopes"
                hint="Permisos solicitados durante el flujo OAuth (ej: repo, read:user)"
              >
                <TokenInput
                  values={form.oauth_scopes}
                  onChange={(vals) => set('oauth_scopes', vals)}
                  placeholder="repo"
                  disabled={submitting}
                />
              </Field>
            )}
          </div>
        )}

        {/* Capabilities */}
        <Field label="Capacidades" hint="Qué expone el servidor MCP">
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 4 }}>
            {CAPABILITY_OPTIONS.map((cap) => (
              <Badge
                key={cap}
                appearance={form.capabilities.includes(cap) ? 'filled' : 'outline'}
                color={form.capabilities.includes(cap) ? 'brand' : 'informative'}
                size="medium"
                style={{ cursor: 'pointer', userSelect: 'none', padding: '4px 10px' }}
                onClick={() => !submitting && toggleCapability(cap)}
              >
                {cap}
              </Badge>
            ))}
          </div>
        </Field>

        {/* Agentes permitidos */}
        <Field
          label="Agentes autorizados"
          hint="Deja vacío para permitir todos. Especifica nombres de agentes para restringir."
        >
          <TokenInput
            values={form.allowed_agents}
            onChange={(vals) => set('allowed_agents', vals)}
            placeholder="TechnicalSupportAgent"
            disabled={submitting}
          />
        </Field>

        {/* Icon URL (opcional) */}
        <Field label="URL del ícono (opcional)">
          <Input
            value={form.icon_url}
            onChange={(_, d) => set('icon_url', d.value)}
            placeholder="https://example.com/icon.png"
            disabled={submitting}
          />
        </Field>
      </div>

      {/* Acciones */}
      <div
        style={{
          display: 'flex',
          justifyContent: 'flex-end',
          gap: 8,
          marginTop: 24,
          paddingTop: 16,
          borderTop: '1px solid var(--colorNeutralStroke2)',
        }}
      >
        <Button appearance="secondary" onClick={onCancel} disabled={submitting}>
          Cancelar
        </Button>
        <Button
          appearance="primary"
          icon={submitting ? <Spinner size="tiny" /> : <Add24Regular />}
          onClick={handleSubmit}
          disabled={submitting}
        >
          {submitting ? 'Registrando...' : 'Agregar servidor'}
        </Button>
      </div>
    </div>
  );
};
