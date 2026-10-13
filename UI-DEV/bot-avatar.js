/* Stable, local SVG companions. No image service or personal data leaves the browser. */
const BotAvatar = (() => {
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const palettes = [
    ['#283d39', '#8edac0', '#e8fff4'],
    ['#49382d', '#edba83', '#fff1db'],
    ['#34394b', '#aebfe9', '#edf2ff'],
    ['#49343c', '#e8a8b8', '#fff0f3'],
    ['#41422e', '#d5d997', '#fbfbdc'],
  ];
  const ears = [
    '<path d="M15 29 14 10 29 21M35 21 50 10 49 29"/>',
    '<ellipse cx="23" cy="17" rx="7" ry="15"/><ellipse cx="41" cy="17" rx="7" ry="15"/>',
    '<circle cx="18" cy="22" r="10"/><circle cx="46" cy="22" r="10"/>',
    '<path d="M14 31 10 13 28 22M36 22 54 13 50 31"/>',
    '<circle cx="20" cy="22" r="9"/><circle cx="44" cy="22" r="9"/>',
  ];
  function seed(id) {
    let hash = 2166136261;
    for (const char of String(id ?? 'bot')) hash = Math.imul(hash ^ char.charCodeAt(0), 16777619);
    return hash >>> 0;
  }
  function render(item, {working = false, historical = false} = {}) {
    const hash = seed(item.id);
    const kind = hash % ears.length;
    const [background, fur, muzzle] = palettes[Math.floor(hash / 5) % palettes.length];
    const face = kind === 3
      ? '<path d="M12 32 26 35 32 47 38 35 52 32Q54 55 32 55 10 55 12 32" fill="var(--pet-muzzle)"/>'
      : '<ellipse cx="32" cy="43" rx="15" ry="11" fill="var(--pet-muzzle)"/>';
    const glasses = (hash >>> 8) % 3 === 0
      ? '<g fill="none" stroke="#293139" stroke-width="2"><circle cx="23" cy="34" r="7"/><circle cx="41" cy="34" r="7"/><path d="M30 34h4"/></g>' : '';
    const tuft = (hash >>> 12) % 2 === 0 ? '<path d="m29 23 3-8 5 8" fill="var(--pet-muzzle)"/>' : '';
    const description = `${item.name || 'Bot'}${working ? ' · Sedang bekerja' : ''}`;
    return `<span class="avatar bot-avatar${working ? ' is-working' : ''}" data-bot-avatar="${escape(item.id)}"${historical ? ' data-avatar-historical="true"' : ''} title="${escape(description)}" aria-hidden="true" style="--pet-bg:${background};--pet-fur:${fur};--pet-muzzle:${muzzle}"><svg viewBox="0 0 64 64" fill="none" focusable="false" aria-hidden="true"><g class="pet-character"><g fill="var(--pet-fur)">${ears[kind]}<rect x="11" y="20" width="42" height="36" rx="19"/></g>${tuft}${face}<g class="pet-eyes" fill="#263238"><ellipse cx="23" cy="34" rx="2.6" ry="3.5"/><ellipse cx="41" cy="34" rx="2.6" ry="3.5"/></g>${glasses}<ellipse cx="17" cy="41" rx="4" ry="2.5" fill="#d47880" opacity=".55"/><ellipse cx="47" cy="41" rx="4" ry="2.5" fill="#d47880" opacity=".55"/><path d="m29 42 3 3 3-3Z" fill="#263238"/><path d="M32 45v2m-5 0q5 6 10 0" stroke="#263238" stroke-width="1.8" stroke-linecap="round"/><path class="pet-paw" d="M44 54q10-14 13-7t-7 12" fill="var(--pet-fur)" stroke="var(--pet-bg)" stroke-width="2"/></g></svg><span class="pet-work-mark"></span></span>`;
  }
  return {render, seed};
})();
if (typeof module !== 'undefined') module.exports = BotAvatar;
