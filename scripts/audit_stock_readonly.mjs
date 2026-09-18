// Read-only audit: public balances and Telegram sticker metadata. Never signs,
// broadcasts transactions, starts the bot, or connects to the project database.
import fs from 'node:fs';
const env = Object.fromEntries(fs.readFileSync('.env', 'utf8').split(/\r?\n/)
  .map(line => line.match(/^\s*([A-Z_0-9]+)\s*=\s*(.*)$/)).filter(Boolean)
  .map(([, key, value]) => [key, value.trim().replace(/^(["'])(.*)\1$/, '$2')]));
async function check(label, url, body, select = data => data) {
  try {
    const response = await fetch(url, {
      method: body ? 'POST' : 'GET',
      headers: { 'Content-Type': 'application/json' },
      ...(body ? { body: JSON.stringify(body) } : {}),
      signal: AbortSignal.timeout(12000),
    });
    let data;
    try { data = await response.json(); } catch { data = {}; }
    console.log(JSON.stringify({ label, http: response.status, data: select(data) }));
  } catch (error) {
    // Do not print URLs or exception messages: Telegram URLs contain a token.
    console.log(JSON.stringify({ label, error: error.cause?.code || error.name }));
  }
}
const rpc = (method, params) => ({ jsonrpc: '2.0', id: 1, method, params });
const jobs = [];
const solNew = 'CpmQtQ73gVNzb3vSzk8CRLktzeU5ttgDcdxi3wbLi1WL';
for (const [name, address] of [['env', env.SOL_WALLET_ADDRESS], ['client', solNew]]) {
  for (const url of ['https://solana-rpc.publicnode.com', 'https://api.mainnet-beta.solana.com', 'https://1rpc.io/solana']) {
    jobs.push(check(`SOL/${name}/${new URL(url).hostname}`, url, rpc('getBalance', [address, { commitment: 'confirmed' }])));
  }
}
for (const [symbol, mint] of Object.entries({USDT: 'Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB', USDC: 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'})) {
  jobs.push(check(`SOL/${symbol}/client`, 'https://api.mainnet-beta.solana.com',
    rpc('getTokenAccountsByOwner', [solNew, { mint }, { encoding: 'jsonParsed' }]),
    data => data.error || data.result?.value?.map(item => item.account.data.parsed.info.tokenAmount)));
}
const aptosAddress = '0x593e9d152c392003e26b11105dc9b8d50ee8a1ed8a6600e09e58fbfb0b0c7dee';
const aptosUrl = 'https://fullnode.mainnet.aptoslabs.com/v1';
jobs.push(check('APT/client/legacy-CoinStore', `${aptosUrl}/accounts/${aptosAddress}/resource/${encodeURIComponent('0x1::coin::CoinStore<0x1::aptos_coin::AptosCoin>')}`,
  null, data => ({ value: data.data?.coin?.value, error_code: data.error_code })));
jobs.push(check('APT/client/coin-balance-view', `${aptosUrl}/view`, {
  function: '0x1::coin::balance', type_arguments: ['0x1::aptos_coin::AptosCoin'], arguments: [aptosAddress],
}));
for (const [label, url] of [['old', 'https://rpc.robinhood.com'], ['official', 'https://rpc.mainnet.chain.robinhood.com']]) {
  jobs.push(check(`ROBINHOOD/${label}/chain-id`, url, rpc('eth_chainId', [])));
  jobs.push(check(`ROBINHOOD/${label}/balance`, url, rpc('eth_getBalance', [env.EVM_WALLET_ADDRESS, 'latest'])));
}
const emojiSource = fs.readFileSync('bot/utils/emojis.py', 'utf8').split('DEFAULT_EMOJI_ALTS')[0];
const mapping = [...emojiSource.matchAll(/"((?:COIN_|NET_)[A-Z_]+)":\s*"(\d+)"/g)];
const emojiIds = [...new Set(mapping.map(match => match[2]))];
if (env.TELEGRAM_BOT_TOKEN) {
  jobs.push(check('TELEGRAM/crypto-emoji-metadata', `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/getCustomEmojiStickers`,
    { custom_emoji_ids: emojiIds }, data => ({ ok: data.ok, error_code: data.error_code,
      stickers: data.result?.map(sticker => ({ keys: mapping.filter(match => match[2] === sticker.custom_emoji_id).map(match => match[1]),
        emoji: sticker.emoji, set_name: sticker.set_name, type: sticker.type })) })));
}
await Promise.allSettled(jobs);
