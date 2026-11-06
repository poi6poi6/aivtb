// Server/provider double for the existing player and actual-console lifecycle
// tests. The production browser transport is exercised separately.
module.exports = function provider(fetcher) {
  let sequence = 0;
  return {
    connect:async () => true,
    async search(query, {signal}) {
      const response = await fetcher('/fixture/bilibili-browser?q=' + encodeURIComponent(query), {signal});
      const fail = (message, code = 'invalid_response') => Object.assign(new Error(message), {code});
      if (!(response.headers?.get('content-type') || '').includes('application/json')) throw fail('查询返回的数据无效。');
      let data;
      try { data = await response.json(); } catch (_) { throw fail('查询返回的数据无法解析。'); }
      if (!response.ok) throw fail(typeof data?.error === 'string' ? data.error : '查询失败。', require('../../brain/static/bilibili-player.js').searchErrorCode(data?.error_code));
      if (!data || !Array.isArray(data.results)) throw fail('查询返回的结果无效。');
      if (data.result_id) return data;
      const seen = new Set();
      const results = data.results.filter(item => /^BV[0-9A-Za-z]{10}$/.test(item?.bvid || '') && !seen.has(item.bvid) && seen.add(item.bvid))
        .map(item => {
          const count = Number.isSafeInteger(item.play_count) && item.play_count >= 0 ? item.play_count : null;
          return {...item,play_count:count,play_count_exact:item.play_count_exact ?? count !== null,
            play_count_lower:item.play_count_lower ?? count,play_count_upper:item.play_count_upper ?? count};
        });
      results.sort((a,b) => a.play_count === null ? (b.play_count === null ? 0 : 1) : b.play_count === null ? -1 : b.play_count-a.play_count);
      let auto_bvid = null, selection_reason = 'empty';
      const bvid = /^BV[0-9A-Za-z]{10}$/.test(query) ? query : query.match(/^https:\/\/www\.bilibili\.com\/video\/(BV[0-9A-Za-z]{10})\?p=\d+$/)?.[1];
      const explicit = bvid && results.find(item => item.bvid === bvid);
      if (explicit) { auto_bvid = explicit.bvid; selection_reason = 'explicit_video'; }
      else if (results.length) {
        if (results.some(item => item.play_count === null)) selection_reason = 'missing_play_count';
        else if (results[0].play_count_lower < 200000) selection_reason = 'below_play_threshold';
        else if (results.slice(1).some(item => results[0].play_count_lower < item.play_count_upper)) selection_reason = 'uncertain_play_count';
        else { auto_bvid=results[0].bvid; selection_reason='highest_play'; }
      }
      return {query,result_id:'fixture-result-'+(++sequence),results,auto_bvid,selection_reason};
    },
  };
};
