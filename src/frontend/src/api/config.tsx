// src/config.js

import { UserInfo, claim } from '@/models';

declare global {
  interface Window {
    appConfig?: Record<string, any>;
    activeUserId?: string;
    userInfo?: UserInfo;
  }
}

export let API_URL: string | null = null;
export let USER_ID: string | null = null;
export let USER_INFO: UserInfo | null = null;

export let config = {
  API_URL: '/api',
  ENABLE_AUTH: false,
};

export function setApiUrl(url: string | null) {
  if (url) {
    API_URL = url.includes('/api') ? url : `${url}/api`;
  }
}
export function setUserInfoGlobal(userInfo: UserInfo | null) {
  if (userInfo) {
    USER_ID = userInfo.user_id || null;
    USER_INFO = userInfo;
  }
}
export function setEnvData(configData: Record<string, any>) {
  if (configData) {
    config.API_URL = configData.API_URL || '';
    config.ENABLE_AUTH = configData.ENABLE_AUTH || false;
  }
}

export function getConfigData() {
  if (!config.API_URL || !config.ENABLE_AUTH) {
    // Check if window.appConfig exists
    if (window.appConfig) {
      setEnvData(window.appConfig);
    }
  }

  return { ...config };
}
export async function getUserInfo(): Promise<UserInfo> {
  try {
    const response = await fetch('/.auth/me');
    if (!response.ok) {
      console.log(
        'No identity provider found. Access to chat will be blocked.'
      );
      return {} as UserInfo;
    }
    const payload = await response.json();
    const userInfo: UserInfo = {
      access_token: payload[0].access_token || '',
      expires_on: payload[0].expires_on || '',
      id_token: payload[0].id_token || '',
      provider_name: payload[0].provider_name || '',
      user_claims: payload[0].user_claims || [],
      user_email: payload[0].user_id || '',
      user_first_last_name:
        payload[0].user_claims?.find((claim: claim) => claim.typ === 'name')
          ?.val || '',
      user_id:
        payload[0].user_claims?.find(
          (claim: claim) =>
            claim.typ ===
            'http://schemas.microsoft.com/identity/claims/objectidentifier'
        )?.val || '',
    };
    return userInfo;
  } catch (e) {
    return {} as UserInfo;
  }
}

/**
 * Ensure the cached EasyAuth access token is fresh before it is forwarded to
 * the backend (where it is used as the OBO assertion). EasyAuth tokens live ~1h;
 * once expired the backend's OBO exchange fails with AADSTS500133. This refreshes
 * the App Service token store (`/.auth/refresh`, using the offline_access refresh
 * token) and re-reads `/.auth/me`, transparently — no logout/login required.
 *
 * Refreshes only when the token is missing or within `skewMs` of expiry, so it is
 * cheap to call before every request.
 */
let _refreshInFlight: Promise<void> | null = null;
const _REAUTH_GUARD_KEY = 'macae_last_reauth';

export async function ensureFreshToken(skewMs: number = 5 * 60 * 1000): Promise<void> {
  const info = getUserInfoGlobal();
  const expMs = info?.expires_on ? Date.parse(info.expires_on) : 0;
  const needsRefresh = !info?.access_token || !expMs || expMs - Date.now() < skewMs;
  if (!needsRefresh) return;

  // Collapse concurrent refreshes (e.g. parallel requests) into one.
  if (!_refreshInFlight) {
    _refreshInFlight = (async () => {
      try {
        await fetch('/.auth/refresh', { credentials: 'include' });
        const fresh = await getUserInfo();
        if (fresh?.access_token) {
          setUserInfoGlobal(fresh);
        }
      } catch (e) {
        console.warn('[auth] /.auth/refresh failed', e);
      } finally {
        _refreshInFlight = null;
      }
    })();
  }
  await _refreshInFlight;

  // App Service does not always issue/store a refresh token for custom-resource
  // scopes, so `/.auth/refresh` can return 403 and the token stays stale → the
  // backend OBO exchange then fails with AADSTS500133. When the token is still
  // (about to be) expired after the refresh attempt, fall back to a silent
  // re-auth: with an active AAD session this is seamless (no password prompt).
  const after = getUserInfoGlobal();
  const afterExpMs = after?.expires_on ? Date.parse(after.expires_on) : 0;
  const stillStale =
    !after?.access_token || !afterExpMs || afterExpMs - Date.now() < 60 * 1000;
  if (stillStale) {
    reauthSilently();
  }
}

/**
 * Silently re-authenticate against EasyAuth, preserving the current location.
 * Used only when the token is effectively expired and `/.auth/refresh` could not
 * renew it. Guarded so it cannot loop (at most once per 2 min).
 */
function reauthSilently(): void {
  try {
    const now = Date.now();
    const last = Number(sessionStorage.getItem(_REAUTH_GUARD_KEY) || 0);
    if (now - last < 2 * 60 * 1000) return; // avoid redirect loops
    sessionStorage.setItem(_REAUTH_GUARD_KEY, String(now));
    const here =
      window.location.pathname + window.location.search + window.location.hash;
    window.location.assign(
      `/.auth/login/aad?post_login_redirect_uri=${encodeURIComponent(here)}`
    );
  } catch (e) {
    console.warn('[auth] silent re-auth redirect failed', e);
  }
}

export function getApiUrl() {
  if (!API_URL) {
    // Check if window.appConfig exists
    if (window.appConfig && window.appConfig.API_URL) {
      setApiUrl(window.appConfig.API_URL);
    }
  }

  if (!API_URL) {
    console.info('API URL not yet configured');
    return null;
  }

  return API_URL;
}
export function getUserInfoGlobal() {
  if (!USER_INFO) {
    // Check if window.userInfo exists
    if (window.userInfo) {
      setUserInfoGlobal(window.userInfo);
    }
  }

  if (!USER_INFO) {
    // console.info('User info not yet configured');
    return null;
  }

  return USER_INFO;
}

export function getUserId(): string {
  // USER_ID = getUserInfoGlobal()?.user_id || null;
  if (!USER_ID) {
    USER_ID = getUserInfoGlobal()?.user_id || null;
  }
  const userId = USER_ID ?? '00000000-0000-0000-0000-000000000000';
  return userId;
}

export function getUserName(): string {
  const info = getUserInfoGlobal();
  return info?.user_email || info?.user_first_last_name || 'dev-user@local';
}

/**
 * Build headers with authentication information
 * @param headers Optional additional headers to merge
 * @returns Combined headers object with authentication
 */
export function headerBuilder(
  headers?: Record<string, string>
): Record<string, string> {
  const userId = getUserId();
  const userName = getUserName();
  const accessToken = getUserInfoGlobal()?.access_token;
  const defaultHeaders: Record<string, string> = {
    'x-ms-client-principal-id': String(userId) || '',
    'x-ms-client-principal-name': userName,
    ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
  };
  return {
    ...defaultHeaders,
    ...(headers ? headers : {}),
  };
}

export const toBoolean = (value: any): boolean => {
  if (typeof value !== 'string') {
    return false;
  }
  return value.trim().toLowerCase() === 'true';
};

const apiConfig = {
  setApiUrl,
  getApiUrl,
  toBoolean,
  getUserId,
  getConfigData,
  setEnvData,
  config,
  USER_ID,
  API_URL,
};

export default apiConfig;
