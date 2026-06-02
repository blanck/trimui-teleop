"""Controller mapping helper.

Run on the TrimUI (or your Mac with a pad plugged in):
    python src/main.py --probe

Move each stick and press each button; the live axis values and pressed-button
indices are shown on screen and printed. Use those numbers to fill in the
"controls" block of settings.json (drive_axis, turn_axis, boost_button, ...).
"""
import pygame


def run():
    pygame.init()
    screen = pygame.display.set_mode((1280, 720))
    pygame.display.set_caption("Controller Probe")
    font = pygame.font.Font(None, 40)
    clock = pygame.time.Clock()

    pygame.joystick.init()
    pad = pygame.joystick.Joystick(0) if pygame.joystick.get_count() else None
    if pad:
        pad.init()

    running = True
    while running:
        for e in pygame.event.get():
            if e.type == pygame.QUIT or (e.type == pygame.KEYDOWN and e.key == pygame.K_ESCAPE):
                running = False
            elif e.type == pygame.JOYBUTTONDOWN:
                print(f"BUTTON {e.button} pressed")

        screen.fill((10, 10, 18))
        lines = []
        if pad:
            lines.append(pad.get_name())
            lines.append(f"axes={pad.get_numaxes()}  buttons={pad.get_numbuttons()}  hats={pad.get_numhats()}")
            lines.append("")
            for i in range(pad.get_numaxes()):
                lines.append(f"axis {i}: {pad.get_axis(i):+.2f}")
            down = [str(i) for i in range(pad.get_numbuttons()) if pad.get_button(i)]
            lines.append("buttons down: " + (", ".join(down) if down else "-"))
        else:
            lines.append("No controller detected")

        y = 20
        for ln in lines:
            screen.blit(font.render(ln, True, (0, 230, 140)), (20, y))
            y += 44

        pygame.display.flip()
        clock.tick(30)

    pygame.quit()


if __name__ == "__main__":
    run()
