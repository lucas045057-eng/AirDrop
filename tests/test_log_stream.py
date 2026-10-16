from runner.log_stream import MASK, MIN_WINDOW, Redactor


def test_known_secret_never_leaks_across_any_output_boundary():
    secret = '0x' + 'ab' * 32
    window = max(len(secret), MIN_WINDOW)
    prefix = 'P' * 256

    for cut_inside_secret in range(1, len(secret)):
        suffix = 'S' * (window - len(secret) + cut_inside_secret)
        redactor = Redactor([secret])
        output = redactor.feed((prefix + secret + suffix).encode()) + redactor.flush()

        assert secret not in output, f'secret leaked at boundary offset {cut_inside_secret}'
        assert output.count(MASK) == 1
