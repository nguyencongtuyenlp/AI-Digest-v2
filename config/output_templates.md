# Product Output Templates

Muc tieu cua file nay:
- Chot "feel" san pham cho Notion page va Telegram truoc khi tune tiep prompt.
- Lam cho output nhin nhu mot AI Daily Digest tra phi, khong phai log ky thuat.

## Notion Page Template

### Name
`[emoji] [short_title]`

### Summarize property
`Y chinh cua tin nay la: [ban chat cua tin] ; gia tri thuc te la [gia tri voi founder/operator/team] ; nhung chi nen hanh dong neu [dieu kien/gioi han].`

### Page body sample

```md
## Executive Note
Y chinh cua tin nay la: ve mat thuc te, [tin nay] co gia tri o cho [gia tri cot loi], nhung chi thuc su huu ich voi [doi tuong/boi canh], va van co friction o [chi phi/ha tang/du lieu/quy trinh].

## Source Snapshot
- Source: Reuters
- Domain: reuters.com
- Published: 2026-03-23T00:00:00+00:00
- Verification: heuristic = yes
- Why it matters: primary reporting, market-level implication

## What Happened
[2-3 doan tom tat dieu gi da xay ra, khong ke lai dai dong]

## Why It Matters
[phan tich gia tri kinh doanh/san pham/van hanh]

## Evidence And Caveats
[fact vs claim, muc do tin cay, gi con thieu]

## Market Reaction
[neu co, neu khong thi ghi ro chua co du lieu cong dong]

## Action For Us
[1 doan ngan: nen lam gi, theo doi gi, bo qua gi]

## Recommendation
[goi y mang tinh san pham/GTM/ops]
```

## Telegram Template

### Daily digest sample

```html
<b>AI Daily Brief | 26/03</b>

Sang nay co 3 tin dang theo doi, trong do 2 tin co nen bang chung tot hon cho team lam san pham AI.

1. <b>💼 Business | Reuters: Trung Quoc dang day manh loi the open-source AI</b>
Y nghia thuc te la canh tranh model se khong chi o chat luong, ma o toc do mo rong ecosystem va kha nang thu hut nha phat trien. Tin nay dang de team theo doi vi tac dong truc tiep den chien luoc open-source va partner model.
<i>reuters.com · Diem 82/100 · Do chac cao</i> · <a href="NOTION_LINK_1">Doc them</a>

2. <b>🚀 Product | CNBC: Google hop tac Agile Robots de day manh AI robotics</b>
Day la tin co gia tri vi no cho thay cuoc dua AI dang mo rong sang robot va ha tang vat ly, khong con dung o chat. Neu team quan tam enterprise automation, day la nhom tin nen deep-dive tiep.
<i>cnbc.com · Diem 76/100 · Do chac cao</i> · <a href="NOTION_LINK_2">Doc them</a>

3. <b>🌍 Society | Google AI tai Viet Nam</b>
Tin huu ich o goc do phan phoi va adoption tai thi truong noi dia, nhung hien nen xem nhu mot market signal hon la co hoi hanh dong ngay.
<i>vnexpress.net · Diem 54/100 · Do chac vua</i> · <a href="NOTION_LINK_3">Doc them</a>

Team uu tien doc 2 tin dau neu can quyet dinh nhanh trong ngay.
```

## Tone Guardrails

- Ngan, chac, khong marketing.
- Luon tach `fact` va `implication`.
- Noi ro `gia tri thuc te` truoc khi noi `y tuong hay`.
- Neu tin yeu thi noi thang la tin yeu; khong co gang lam no nghe quan trong.
- Telegram phai de doc trong 30-45 giay.
