// The standalone page shares player and cancellation rules with the console.
(() => {
  const deck = new BilibiliDeck();
  window.bilibiliDeck = deck;
  const query = document.getElementById('bilibili-query');
  const consoleLink = document.getElementById('bilibili-console');
  const updateExternalSearch = query.oninput;
  const updateConsoleLink = () => {
    if (consoleLink) consoleLink.href = '/?bilibili=' + encodeURIComponent(query.value.trim());
  };
  query.oninput = () => { updateExternalSearch?.(); updateConsoleLink(); };
  const initial = new URLSearchParams(location.search).get('q');
  if (initial) deck.request(initial);
  updateConsoleLink();
})();
