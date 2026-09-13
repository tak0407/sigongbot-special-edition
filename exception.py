class BotException(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(self.message)


class ClientException(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(self.message)


class RetrospectiveAlreadySubmitted(BotException):
    """같은 회차에 이미 저장된 회고가 있을 때 발생합니다."""

    def __init__(self, message: str = "이미 이번 회차 회고가 저장되어 있습니다.") -> None:
        super().__init__(message)
