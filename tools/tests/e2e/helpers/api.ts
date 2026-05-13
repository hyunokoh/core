/**
 * Thin API client wrappers that the tests share. Centralizes the bearer
 * header dance and gives a single chokepoint to add logging / retries.
 */
import type { APIRequestContext } from '@playwright/test';

export async function apiGet(
  request: APIRequestContext,
  path: string,
  token?: string,
) {
  return request.get(path, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
}

export async function apiPost(
  request: APIRequestContext,
  path: string,
  body: unknown,
  token?: string,
) {
  return request.post(path, {
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    data: body,
  });
}

/**
 * Pull /auth/me for the current token. Useful when a test mutates user
 * state via the UI and wants to assert the server-side outcome.
 */
export async function getMe(request: APIRequestContext, token: string) {
  const r = await apiGet(request, '/auth/me', token);
  if (!r.ok()) throw new Error(`auth/me failed: ${r.status()}`);
  return (await r.json()).user;
}
