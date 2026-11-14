import random

class Durak:
    def __init__(self, players):
        self.players = players
        deck = [(r, s) for r in range(9) for s in range(4)]
        random.shuffle(deck)
        self.deck = deck
        self.trump = deck[0][1]
        self.hands = {p: [] for p in players}
        for p in players:
            for _ in range(6):
                self.hands[p].append(self.deck.pop())
        self.out = []
        self.attacker = self.first_attacker()
        self.table = []
        self.passed = set()
        self.over = False
        self.msg = ""

    def first_attacker(self):
        best, who = None, self.players[0]
        for p in self.players:
            for r, s in self.hands[p]:
                if s == self.trump and (best is None or r < best):
                    best, who = r, p
        return who

    def active(self):
        return [p for p in self.players if p not in self.out]

    def next_after(self, p):
        i = self.players.index(p)
        n = len(self.players)
        for k in range(1, n + 1):
            q = self.players[(i + k) % n]
            if q not in self.out:
                return q
        return p

    def defender(self):
        return self.next_after(self.attacker)

    def beats(self, att, dfn):
        if att[1] == dfn[1]:
            return dfn[0] > att[0]
        return dfn[1] == self.trump

    def can_add(self, card):
        if len(self.table) >= 6:
            return False
        open_cards = sum(1 for a, d in self.table if d is None)
        if open_cards >= len(self.hands[self.defender()]):
            return False
        if not self.table:
            return True
        ranks = {c[0] for pair in self.table for c in pair if c}
        return card[0] in ranks

    def can_throw(self, p):
        if p == self.defender() or p in self.out:
            return False
        return any(self.can_add(c) for c in self.hands[p])

    def round_over(self):
        if not self.table or any(b is None for a, b in self.table):
            return False
        if self.attacker not in self.passed:
            return False
        d = self.defender()
        for p in self.active():
            if p == d:
                continue
            if p not in self.passed and self.can_throw(p):
                return False
        return True

    def refill(self, d):
        i = self.players.index(self.attacker)
        n = len(self.players)
        order = []
        for k in range(n):
            p = self.players[(i + k) % n]
            if p != d and p not in self.out:
                order.append(p)
        order.append(d)
        for p in order:
            while len(self.hands[p]) < 6 and self.deck:
                self.hands[p].append(self.deck.pop())

    def end_round(self, took):
        d = self.defender()
        if took:
            for a, b in self.table:
                self.hands[d].append(a)
                if b:
                    self.hands[d].append(b)
        self.table = []
        self.passed = set()
        self.refill(d)
        newly = []
        if not self.deck:
            for p in self.active():
                if not self.hands[p]:
                    self.out.append(p)
                    newly.append(p)
        if took or d in self.out:
            self.attacker = self.next_after(d)
        else:
            self.attacker = d
        self.msg = (d + " kartalarni oldi") if took else "Bito"
        if newly:
            self.msg = ", ".join(newly) + " kartalarini tugatdi"

    def check_end(self):
        act = self.active()
        if len(act) <= 1:
            self.over = True
            if not act:
                self.msg = "Durang!"
            else:
                self.msg = act[0] + " durak! 🤡"

    def act(self, who, action, card=None):
        if self.over:
            return "O'yin tugagan"
        if who in self.out:
            return "Siz o'yindan chiqqansiz"
        card = tuple(card) if card else None
        d = self.defender()
        if action == "attack":
            if who == d:
                return "Himoyalanuvchi hujum qila olmaydi"
            if not self.table and who != self.attacker:
                return "Birinchi kartani hujumchi tashlaydi"
            if card not in self.hands[who]:
                return "Bunday karta yo'q"
            if not self.can_add(card):
                return "Bu kartani tashlab bo'lmaydi"
            self.hands[who].remove(card)
            self.table.append([card, None])
            self.passed = set()
            self.msg = ""
        elif action == "defend":
            if who != d:
                return "Siz himoyalanuvchi emassiz"
            if card not in self.hands[who]:
                return "Bunday karta yo'q"
            target = None
            for pr in self.table:
                if pr[1] is None and self.beats(pr[0], card):
                    target = pr
                    break
            if target is None:
                return "Bu karta hech birini yopa olmaydi"
            self.hands[who].remove(card)
            target[1] = card
            self.msg = ""
        elif action == "take":
            if who != d or not self.table:
                return "Hozir olib bo'lmaydi"
            self.end_round(True)
        elif action == "done":
            if who == d or not self.table:
                return "Hozir bito qilib bo'lmaydi"
            self.passed.add(who)
        if self.table and self.round_over():
            self.end_round(False)
        self.check_end()
        return None

    def state(self, who):
        hand = sorted(self.hands[who], key=lambda c: (c[1] == self.trump, c[0]))
        others = [{"name": p, "n": len(self.hands[p]), "out": p in self.out}
                  for p in self.players if p != who]
        return {
            "type": "game",
            "hand": hand,
            "others": others,
            "deck_n": len(self.deck),
            "trump": self.trump,
            "table": self.table,
            "attacker": self.attacker,
            "defender": self.defender(),
            "out": who in self.out,
            "over": self.over,
            "msg": self.msg,
        }