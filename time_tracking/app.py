import sqlite3
from datetime import date, timedelta
from pathlib import Path

import AppKit as kit
import objc
from Foundation import NSAttributedString, NSDate, NSLocale, NSObject, NSRunLoop, NSRunLoopCommonModes, NSTimer

from time_tracking.tracker import (
    MICROSECONDS_PER_SECOND,
    TimeTracker,
    TrackerAlreadyOpen,
    format_duration,
    start_of_day,
)


APP_NAME = "Учёт времени"
DB_PATH = Path.home() / "Library" / "Application Support" / "Time Tracking" / "time_tracking.sqlite3"
COMPACT_HINT = "Правый клик или ⌘⇧M — развернуть окно."
RUNNING_COLOR = kit.NSColor.colorWithSRGBRed_green_blue_alpha_(222 / 255, 94 / 255, 216 / 255, 1)
STOPPED_COLOR = kit.NSColor.colorWithSRGBRed_green_blue_alpha_(70 / 255, 226 / 255, 73 / 255, 1)


def label(view, title, frame, size=13, weight=kit.NSFontWeightRegular, *, secondary=False, digits=False):
    field = kit.NSTextField.labelWithString_(title)
    font = (
        kit.NSFont.monospacedDigitSystemFontOfSize_weight_
        if digits
        else kit.NSFont.systemFontOfSize_weight_
    )
    field.setFont_(font(size, weight))
    if secondary:
        field.setTextColor_(kit.NSColor.secondaryLabelColor())
    field.setFrame_(frame)
    view.addSubview_(field)
    return field


class RoundedButton(kit.NSButton):
    def drawRect_(self, rect):
        color = self.fill_color
        if self.isHighlighted():
            color = color.blendedColorWithFraction_ofColor_(0.12, kit.NSColor.blackColor())
        color.setFill()
        kit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            self.bounds(), self.corner_radius, self.corner_radius
        ).fill()
        objc.super(RoundedButton, self).drawRect_(rect)

    @objc.python_method
    def set_style(self, title, color):
        if self.title() == title and self.fill_color == color:
            return
        self.fill_color = color
        attributes = {
            kit.NSFontAttributeName: self.font(),
            kit.NSForegroundColorAttributeName: self.text_color,
        }
        self.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(title, attributes))
        self.setNeedsDisplay_(True)


def button(view, title, frame, target, action, *, font_size, color, text_color, corner_radius=8):
    control = RoundedButton.buttonWithTitle_target_action_("", target, action)
    control.fill_color = None
    control.text_color = text_color
    control.corner_radius = corner_radius
    control.setBordered_(False)
    control.setFont_(kit.NSFont.systemFontOfSize_weight_(font_size, kit.NSFontWeightSemibold))
    control.set_style(title, color)
    control.setFrame_(frame)
    view.addSubview_(control)
    return control


def card(view, frame):
    box = kit.NSBox.alloc().initWithFrame_(frame)
    box.setBoxType_(kit.NSBoxCustom)
    box.setContentViewMargins_((0, 0))
    box.setCornerRadius_(12)
    box.setBorderWidth_(0)
    box.setFillColor_(kit.NSColor.controlBackgroundColor())
    view.addSubview_(box)
    return box.contentView()


def date_picker(view, initial_date, name, frame, target):
    picker = kit.NSDatePicker.alloc().initWithFrame_(frame)
    picker.setDatePickerStyle_(kit.NSDatePickerStyleTextFieldAndStepper)
    picker.setDatePickerElements_(kit.NSDatePickerElementFlagYearMonthDay)
    picker.setLocale_(NSLocale.localeWithLocaleIdentifier_("ru_RU"))
    initial_timestamp = start_of_day(initial_date) / MICROSECONDS_PER_SECOND
    picker.setDateValue_(NSDate.dateWithTimeIntervalSince1970_(initial_timestamp))
    picker.setAccessibilityLabel_(name)
    picker.setTarget_(target)
    picker.setAction_("periodChanged:")
    view.addSubview_(picker)
    return picker


def show_alert(message, detail):
    alert = kit.NSAlert.alloc().init()
    alert.setAlertStyle_(kit.NSAlertStyleWarning)
    alert.setMessageText_(message)
    alert.setInformativeText_(detail)
    alert.addButtonWithTitle_("ОК")
    alert.runModal()


class CompactPanel(kit.NSPanel):
    def canBecomeKeyWindow(self):
        return True


class AppController(NSObject):
    @objc.python_method
    def configure(self, app, tracker):
        self.app = app
        self.tracker = tracker
        self.timer = None
        self.compact = False
        self._build_main_window()
        self._build_compact_panel()
        self._build_menus()

    @objc.python_method
    def _build_main_window(self):
        style = kit.NSWindowStyleMaskTitled | kit.NSWindowStyleMaskClosable | kit.NSWindowStyleMaskMiniaturizable
        content_width = 648
        self.window = kit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            ((0, 0), (content_width + 48, 444)), style, kit.NSBackingStoreBuffered, False
        )
        self.window.setReleasedWhenClosed_(False)
        self.window.setTitle_(APP_NAME)
        self.window.setDelegate_(self)
        self.window.center()
        view = self.window.contentView()

        label(view, "Рабочее время", ((24, 392), (275, 33)), 27, kit.NSFontWeightSemibold)
        self.status = label(view, "", ((25, 363), (330, 20)), secondary=True)
        compact = button(
            view, "Компактно", ((504, 386), (168, 45)), self, "switchView:",
            font_size=19.5, color=kit.NSColor.controlBackgroundColor(), text_color=kit.NSColor.labelColor(),
        )
        compact.setToolTip_("Свернуть окно до кнопки таймера")

        for index, title in enumerate(("Сегодня", "На этой неделе")):
            summary = card(view, ((24 + index * 330, 266), (318, 79)))
            label(summary, title, ((16, 51), (179, 19)), secondary=True)
            value = label(summary, "", ((16, 12), (286, 33)), 25, kit.NSFontWeightSemibold, digits=True)
            if index == 0:
                self.today_total = value
            else:
                self.week_total = value

        report = card(view, ((24, 110), (content_width, 144)))
        report.scaleUnitSquareToSize_((1.5, 1.5))
        label(report, "За выбранный период", ((16, 59), (239, 23)), 15, kit.NSFontWeightSemibold)
        self.period_total = label(report, "", ((256, 55), (160, 31)), 22, kit.NSFontWeightSemibold, digits=True)
        self.period_total.setAlignment_(kit.NSTextAlignmentRight)
        today = date.today()
        label(report, "С", ((16, 23), (15, 20)), secondary=True)
        self.first_date = date_picker(report, today.replace(day=1), "Начало периода", ((35, 20), (137, 24)), self)
        label(report, "по", ((188, 23), (19, 20)), secondary=True)
        self.last_date = date_picker(report, today, "Конец периода", ((211, 20), (137, 24)), self)
        self.period_error = label(report, "", ((16, 2), (400, 17)), 11)
        self.period_error.setTextColor_(kit.NSColor.systemRedColor())

        self.main_button = button(
            view, "", ((24, 16), (content_width, 76)), self, "toggleTimer:",
            font_size=26, color=STOPPED_COLOR, text_color=kit.NSColor.blackColor(),
        )

    @objc.python_method
    def _build_compact_panel(self):
        button_frame = ((0, 0), (272, 76))
        self.panel = CompactPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            button_frame, kit.NSWindowStyleMaskBorderless, kit.NSBackingStoreBuffered, False
        )
        self.panel.setTitle_(APP_NAME)
        self.panel.setOpaque_(False)
        self.panel.setBackgroundColor_(kit.NSColor.clearColor())
        self.panel.setHasShadow_(False)
        self.panel.setFloatingPanel_(True)
        self.panel.setHidesOnDeactivate_(False)
        self.panel.setMovableByWindowBackground_(True)
        self.panel.setCollectionBehavior_(kit.NSWindowCollectionBehaviorMoveToActiveSpace)
        self.compact_button = button(
            self.panel.contentView(), "", button_frame, self, "toggleTimer:",
            font_size=26, color=STOPPED_COLOR, text_color=kit.NSColor.blackColor(), corner_radius=3,
        )

    @objc.python_method
    def _build_menus(self):
        menu_bar = kit.NSMenu.alloc().init()
        application_menu = kit.NSMenu.alloc().initWithTitle_(APP_NAME)
        application_menu.addItemWithTitle_action_keyEquivalent_(f"Завершить «{APP_NAME}»", "terminate:", "q")
        menu_bar.addItem_(kit.NSMenuItem.alloc().init())
        menu_bar.itemAtIndex_(0).setSubmenu_(application_menu)

        view_menu = kit.NSMenu.alloc().initWithTitle_("Вид")
        self.view_item = view_menu.addItemWithTitle_action_keyEquivalent_("Компактный режим", "switchView:", "m")
        self.view_item.setTarget_(self)
        self.view_item.setKeyEquivalentModifierMask_(kit.NSEventModifierFlagCommand | kit.NSEventModifierFlagShift)
        view_item = kit.NSMenuItem.alloc().init()
        menu_bar.addItem_(view_item)
        view_item.setSubmenu_(view_menu)
        self.app.setMainMenu_(menu_bar)

        panel_menu = kit.NSMenu.alloc().init()
        expand = panel_menu.addItemWithTitle_action_keyEquivalent_("Развернуть окно", "switchView:", "")
        expand.setTarget_(self)
        panel_menu.addItemWithTitle_action_keyEquivalent_("Завершить работу", "terminate:", "")
        self.compact_button.setMenu_(panel_menu)

    @objc.python_method
    def _set_compact(self, compact):
        self.compact = compact
        if compact:
            top_left = self.window.frame()
            self.panel.setFrameTopLeftPoint_((top_left.origin.x, top_left.origin.y + top_left.size.height))
            self.panel.makeKeyAndOrderFront_(self)
            self.window.orderOut_(self)
        else:
            self.window.deminiaturize_(self)
            self.window.makeKeyAndOrderFront_(self)
            self.panel.orderOut_(self)
        self.view_item.setTitle_("Развернуть окно" if compact else "Компактный режим")
        self.tick_(None)

    @objc.python_method
    def _refresh(self):
        running = self.tracker.is_running
        color = RUNNING_COLOR if running else STOPPED_COLOR
        self.main_button.set_style("Остановить таймер" if running else "Начать отсчёт", color)
        self.compact_button.set_style("Отдыхать" if running else "Работать", color)
        self.compact_button.setToolTip_(COMPACT_HINT)
        self.status.setStringValue_("Идёт учёт времени" if running else "Таймер остановлен")
        self.status.setTextColor_(kit.NSColor.systemGreenColor() if running else kit.NSColor.secondaryLabelColor())
        if self.compact:
            return

        today = date.today()
        monday = today - timedelta(days=today.weekday())
        self.today_total.setStringValue_(format_duration(self.tracker.total(today, today)))
        self.week_total.setStringValue_(format_duration(self.tracker.total(monday, monday + timedelta(days=6))))
        first = date.fromtimestamp(self.first_date.dateValue().timeIntervalSince1970())
        last = date.fromtimestamp(self.last_date.dateValue().timeIntervalSince1970())
        if first > last:
            self.period_total.setStringValue_("—")
            self.period_error.setStringValue_("Дата начала должна быть раньше даты конца или совпадать с ней.")
        else:
            self.period_total.setStringValue_(format_duration(self.tracker.total(first, last)))
            self.period_error.setStringValue_("")

    @objc.IBAction
    def switchView_(self, sender):
        self._set_compact(not self.compact)

    @objc.IBAction
    def periodChanged_(self, sender):
        self.tick_(None)

    @objc.IBAction
    def toggleTimer_(self, sender):
        try:
            if self.tracker.is_running:
                self.tracker.stop()
            else:
                self.tracker.start()
        except sqlite3.Error as error:
            show_alert("Не удалось изменить состояние таймера", str(error))
        self.tick_(None)

    @objc.IBAction
    def tick_(self, sender):
        try:
            self.tracker.checkpoint()
            self._refresh()
        except sqlite3.Error:
            self.status.setStringValue_("Ошибка доступа к базе данных")
            self.status.setTextColor_(kit.NSColor.systemRedColor())
            self.compact_button.setToolTip_(f"Ошибка доступа к базе данных. {COMPACT_HINT}")

    def applicationDidFinishLaunching_(self, notification):
        self._set_compact(False)
        self.app.activateIgnoringOtherApps_(True)
        self.timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(1, self, "tick:", None, True)
        NSRunLoop.mainRunLoop().addTimer_forMode_(self.timer, NSRunLoopCommonModes)

    def applicationShouldHandleReopen_hasVisibleWindows_(self, app, visible):
        self._set_compact(False)
        return True

    def windowShouldClose_(self, window):
        self.app.terminate_(self)
        return False

    @objc.typedSelector(b"Q@:@")
    def applicationShouldTerminate_(self, app):
        try:
            self.tracker.close()
        except sqlite3.Error as error:
            show_alert("Не удалось сохранить время перед выходом", str(error))
            return kit.NSTerminateCancel
        if self.timer is not None:
            self.timer.invalidate()
        return kit.NSTerminateNow


def main():
    app = kit.NSApplication.sharedApplication()
    app.setActivationPolicy_(kit.NSApplicationActivationPolicyRegular)
    try:
        tracker = TimeTracker(DB_PATH)
    except TrackerAlreadyOpen:
        show_alert("Приложение уже запущено", "Откройте работающее окно через Dock.")
        return 1
    except (OSError, sqlite3.Error) as error:
        show_alert("Не удалось открыть базу данных", str(error))
        return 1

    controller = AppController.alloc().init()
    controller.configure(app, tracker)
    app.setDelegate_(controller)
    app.run()
    return 0
