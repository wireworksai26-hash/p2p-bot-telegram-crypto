// Pencocokan nominal transaksi GoPay vs tagihan.
//
// GoPay mengirim gross_amount dalam sen; kelipatan 100 dibagi 100. Hanya ada
// SATU tafsiran nominal per transaksi: dulu raw juga dibandingkan langsung
// sehingga bayar Rp 1.000 (raw 100000) melunasi tagihan Rp 100.000.
// Bot tidak pernah membuat tagihan kelipatan 100, jadi tafsiran ini benar baik
// bila satuan GoPay sen maupun rupiah.
function rawAmount(tx) {
    return parseInt(tx.gross_amount || tx.real_gross_amount || tx.amount?.value || tx.amount || 0, 10);
}

function txAmountIdr(tx) {
    const rawAmt = rawAmount(tx);
    return (rawAmt > 0 && rawAmt % 100 === 0) ? Math.round(rawAmt / 100) : rawAmt;
}

function amountMatches(tx, targetAmount) {
    const target = parseInt(targetAmount, 10);
    return Number.isFinite(target) && target > 0 && txAmountIdr(tx) === target;
}

module.exports = { rawAmount, txAmountIdr, amountMatches };
