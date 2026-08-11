// twin_reader.js — 브라우저에서 .twin(ZIP) 을 통째로 읽지 않고 필요한 부분만 꺼낸다.
//
// 단일 HTML 에 모든 사진을 base64 로 심으면 파일이 폭증한다(사진 594장 보고서 = 651MB,
// 그중 83%가 사진). 게다가 base64 는 +33% 이고 브라우저가 문서 전체를 파싱해야 한다.
//
// 대신 .twin 파일 자체를 <input type="file"> 로 받으면, File 은 디스크 상의 참조라서
// slice(시작,끝) 로 필요한 바이트 범위만 읽을 수 있다. .twin 안의 사진과 메시는 무압축
// (STORED)으로 들어 있어, 그 범위를 그대로 Blob 으로 감싸면 복사도 압축 해제도 없다.
// 사진은 실제로 보는 1~2장만 디스크에서 읽히므로 메모리에 650MB 를 올릴 일이 없다.
//
// meta.json 만 DEFLATE 다(구 버전 보고서). DecompressionStream 으로 푼다. 새 보고서는
// 파이프라인이 무압축으로 저장하므로 이 경로를 타지 않는다.

const EOCD_SIG = 0x06054b50;
const EOCD64_SIG = 0x06064b50;
const LOC64_SIG = 0x07064b50;
const CEN_SIG = 0x02014b50;

async function buf(file, start, end) {
  return new DataView(await file.slice(start, end).arrayBuffer());
}

/**
 * .twin(ZIP) 을 열어 엔트리 목록을 읽는다. 데이터는 이 시점에 읽지 않는다.
 * @param {File|Blob} file
 */
export async function openTwin(file) {
  // ── EOCD 찾기: 끝에서부터 뒤진다(주석이 붙어 있을 수 있어 위치가 고정이 아니다) ──
  const tailLen = Math.min(file.size, 65557);          // 22 + 최대 주석 65535
  const tail = await buf(file, file.size - tailLen, file.size);
  let eocd = -1;
  for (let i = tailLen - 22; i >= 0; i--) {
    if (tail.getUint32(i, true) === EOCD_SIG) { eocd = i; break; }
  }
  if (eocd < 0) throw new Error('ZIP 형식이 아닙니다 (.twin 파일이 맞는지 확인하세요)');

  let nEntries = tail.getUint16(eocd + 10, true);
  let cenSize = tail.getUint32(eocd + 12, true);
  let cenOff = tail.getUint32(eocd + 16, true);

  // ZIP64 — 4GB/65535개를 넘으면 값이 0xFFFF/0xFFFFFFFF 로 채워지고 실제 값은 별도 레코드에 있다
  if (cenOff === 0xffffffff || cenSize === 0xffffffff || nEntries === 0xffff) {
    let loc = -1;
    for (let i = eocd - 20; i >= 0; i--) {
      if (tail.getUint32(i, true) === LOC64_SIG) { loc = i; break; }
    }
    if (loc < 0) throw new Error('ZIP64 레코드를 찾을 수 없습니다');
    const e64Off = Number(tail.getBigUint64(loc + 8, true));
    const d = await buf(file, e64Off, e64Off + 56);
    if (d.getUint32(0, true) !== EOCD64_SIG) throw new Error('ZIP64 EOCD 손상');
    nEntries = Number(d.getBigUint64(32, true));
    cenSize = Number(d.getBigUint64(40, true));
    cenOff = Number(d.getBigUint64(48, true));
  }

  // ── 중앙 디렉터리 파싱 ──
  const cen = await buf(file, cenOff, cenOff + cenSize);
  const dec = new TextDecoder('utf-8');
  const entries = new Map();
  let o = 0;
  for (let k = 0; k < nEntries; k++) {
    if (cen.getUint32(o, true) !== CEN_SIG) break;
    const method = cen.getUint16(o + 10, true);
    let cSize = cen.getUint32(o + 20, true);
    let uSize = cen.getUint32(o + 24, true);
    const nameLen = cen.getUint16(o + 28, true);
    const extraLen = cen.getUint16(o + 30, true);
    const cmtLen = cen.getUint16(o + 32, true);
    let localOff = cen.getUint32(o + 42, true);
    const name = dec.decode(new Uint8Array(cen.buffer, cen.byteOffset + o + 46, nameLen));

    // ZIP64 확장 필드(0x0001) — 0xFFFFFFFF 인 항목만 순서대로 8바이트로 들어 있다
    if (uSize === 0xffffffff || cSize === 0xffffffff || localOff === 0xffffffff) {
      let p = o + 46 + nameLen;
      const end = p + extraLen;
      while (p + 4 <= end) {
        const id = cen.getUint16(p, true), sz = cen.getUint16(p + 2, true);
        if (id === 0x0001) {
          let q = p + 4;
          if (uSize === 0xffffffff) { uSize = Number(cen.getBigUint64(q, true)); q += 8; }
          if (cSize === 0xffffffff) { cSize = Number(cen.getBigUint64(q, true)); q += 8; }
          if (localOff === 0xffffffff) { localOff = Number(cen.getBigUint64(q, true)); }
          break;
        }
        p += 4 + sz;
      }
    }
    entries.set(name, { method, cSize, uSize, localOff, dataOff: -1 });
    o += 46 + nameLen + extraLen + cmtLen;
  }

  /** 압축 데이터가 실제로 시작하는 위치. 로컬 헤더의 이름·확장 길이는 중앙 것과 다를 수 있어 직접 읽는다. */
  async function dataOffset(e) {
    if (e.dataOff < 0) {
      const h = await buf(file, e.localOff, e.localOff + 30);
      e.dataOff = e.localOff + 30 + h.getUint16(26, true) + h.getUint16(28, true);
    }
    return e.dataOff;
  }

  function get(name) {
    const e = entries.get(name);
    if (!e) throw new Error('보고서에 없는 항목: ' + name);
    return e;
  }

  /** 엔트리를 Blob 으로. STORED 면 디스크 참조 그대로라 메모리를 쓰지 않는다. */
  async function blob(name, mime) {
    const e = get(name);
    const off = await dataOffset(e);
    const raw = file.slice(off, off + e.cSize, e.method === 0 ? (mime || '') : '');
    if (e.method === 0) return raw;
    if (e.method !== 8) throw new Error('지원하지 않는 압축 방식: ' + e.method);
    if (typeof DecompressionStream !== 'function')
      throw new Error('이 브라우저는 압축 해제를 지원하지 않습니다. 최신 브라우저에서 열어 주세요.');
    const ds = new DecompressionStream('deflate-raw');
    return new Response(raw.stream().pipeThrough(ds)).blob()
      .then(b => (mime ? b.slice(0, b.size, mime) : b));
  }

  return {
    entries,
    has: (name) => entries.has(name),
    size: (name) => get(name).uSize,
    blob,
    /** <img src> 에 바로 쓸 URL. 다 쓰면 revokeObjectURL 로 돌려줘야 한다. */
    url: async (name, mime) => URL.createObjectURL(await blob(name, mime)),
    bytes: async (name) => (await blob(name)).arrayBuffer(),
    text: async (name) => (await blob(name)).text(),
    json: async (name) => JSON.parse(await (await blob(name)).text()),
  };
}
