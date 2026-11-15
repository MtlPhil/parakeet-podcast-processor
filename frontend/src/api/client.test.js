import { afterEach, describe, expect, it, vi } from 'vitest';
import { filenameFromResponse, getMe, getPodcast, updatePodcast } from './client';

const response = (status, body, headers = {}) =>
  new Response(body === undefined ? null : JSON.stringify(body), { status, headers });

afterEach(() => vi.unstubAllGlobals());

describe('API requests', () => {
  it('sends JSON to /api and reads the reply', async () => {
    const fetch = vi.fn(async () => response(200, { id: 3 }));
    vi.stubGlobal('fetch', fetch);

    expect(await updatePodcast(3, { title: 'New' })).toEqual({ id: 3 });
    const [url, options] = fetch.mock.calls[0];
    expect(url).toBe('/api/podcasts/3');
    expect(options.method).toBe('PATCH');
    expect(options.headers['Content-Type']).toBe('application/json');
    expect(JSON.parse(options.body)).toEqual({ title: 'New' });
  });

  it('reads No Content as null', async () => {
    vi.stubGlobal('fetch', async () => response(204));
    expect(await getMe()).toBeNull();
  });

  it("raises the server's detail with the status", async () => {
    vi.stubGlobal('fetch', async () => response(404, { detail: 'Podcast not found' }));
    await expect(getPodcast(9)).rejects.toMatchObject({
      message: 'Podcast not found',
      status: 404,
    });
  });

  it('lists each rejected field with its problem', async () => {
    const detail = [
      { loc: ['body', 'title'], msg: 'Field required' },
      { loc: [], msg: 'Bad request' },
    ];
    vi.stubGlobal('fetch', async () => response(422, { detail }));
    await expect(getMe()).rejects.toThrow('title: Field required; Bad request');
  });

  it('says the status when the reply has no detail', async () => {
    vi.stubGlobal('fetch', async () => new Response('Bad Gateway', { status: 502 }));
    await expect(getMe()).rejects.toThrow('API error 502');
  });
});

describe('filenameFromResponse', () => {
  const named = (value) => response(200, {}, { 'Content-Disposition': value });

  it('takes the percent-encoded name over the ASCII one', () => {
    const res = named(`attachment; filename="Ca va.md"; filename*=UTF-8''%C3%87a%20va.md`);
    expect(filenameFromResponse(res, 'x.md')).toBe('Ça va.md');
  });

  it('falls back to the plain name when the encoded one is malformed', () => {
    const res = named(`attachment; filename="plain.md"; filename*=UTF-8''%E0%A4%A.md`);
    expect(filenameFromResponse(res, 'x.md')).toBe('plain.md');
  });

  it('uses the fallback without a header', () => {
    expect(filenameFromResponse(response(200, {}), 'x.md')).toBe('x.md');
  });
});
