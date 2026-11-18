"""Oddiy ikki kishilik o'yinlar. Har bir o'yin sinfi Durak bilan bir xil interfeysga ega:
players, over, msg, state(ism), act(ism, amal, argument) -> xato matni yoki None."""

LINES = [(0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6)]


class XO:
    def __init__(self, players):
        self.players = list(players)[:2]
        self.board = [""] * 9
        self.turn = 0
        self.over = False
        self.msg = ""
        self.winner = None
        self.line = None

    def state(self, name):
        p = self.players
        return {"type": "game", "kind": "xo", "players": p, "marks": {p[0]: "X", p[1]: "O"},
                "board": self.board, "turn": p[self.turn], "over": self.over, "winner": self.winner,
                "line": self.line, "msg": self.msg}

    def act(self, name, action, arg):
        if self.over:
            return "O'yin tugagan"
        if action != "move":
            return "Noto'g'ri amal"
        if name != self.players[self.turn]:
            return "Navbat sizniki emas"
        try:
            i = int(arg)
        except (TypeError, ValueError):
            return "Noto'g'ri katak"
        if not 0 <= i < 9:
            return "Noto'g'ri katak"
        if self.board[i]:
            return "Bu katak band"
        mark = "X" if self.turn == 0 else "O"
        self.board[i] = mark
        for a, b, c in LINES:
            if self.board[a] == self.board[b] == self.board[c] == mark:
                self.over = True
                self.winner = name
                self.line = [a, b, c]
                self.msg = name + " yutdi! 🎉"
                return None
        if all(self.board):
            self.over = True
            self.msg = "Durang!"
            return None
        self.turn = 1 - self.turn
        return None


BEATS = {"rock": "scissors", "scissors": "paper", "paper": "rock"}


class RPS:
    """Tosh-qaychi-qog'oz: kim birinchi bo'lib 2 raund yutsa, g'olib."""
    WIN = 2

    def __init__(self, players):
        self.players = list(players)[:2]
        self.score = {p: 0 for p in self.players}
        self.round = 1
        self.picks = {}
        self.last = None
        self.over = False
        self.msg = ""
        self.winner = None

    def state(self, name):
        return {"type": "game", "kind": "rps", "players": self.players, "score": self.score,
                "round": self.round, "picked": {p: (p in self.picks) for p in self.players},
                "mine": self.picks.get(name), "last": self.last, "over": self.over,
                "winner": self.winner, "msg": self.msg, "target": self.WIN}

    def act(self, name, action, arg):
        if self.over:
            return "O'yin tugagan"
        if action != "pick" or arg not in BEATS:
            return "Tanlov noto'g'ri"
        if name in self.picks:
            return "Siz tanlab bo'ldingiz"
        self.picks[name] = arg
        if len(self.picks) == 2:
            a, b = self.players
            ca, cb = self.picks[a], self.picks[b]
            w = None if ca == cb else (a if BEATS[ca] == cb else b)
            if w:
                self.score[w] += 1
            self.last = {"round": self.round, "choices": {a: ca, b: cb}, "winner": w}
            self.picks = {}
            self.round += 1
            if w and self.score[w] >= self.WIN:
                self.over = True
                self.winner = w
                self.msg = w + " yutdi! 🎉"
        return None
