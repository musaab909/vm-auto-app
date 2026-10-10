import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';

import 'screens/home_shell.dart';
import 'screens/onboarding_screen.dart';
import 'services/app_state.dart';
import 'services/notifications.dart';
import 'theme.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final state = AppState();
  await state.load();
  try {
    await NotificationService.instance.init();
  } catch (_) {}
  runApp(WaterApp(state: state));
}

class WaterApp extends StatelessWidget {
  final AppState state;
  const WaterApp({super.key, required this.state});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'مذكر شرب الماء المدرسي',
      debugShowCheckedModeBanner: false,
      theme: buildTheme(),
      locale: const Locale('ar'),
      supportedLocales: const [Locale('ar')],
      localizationsDelegates: const [
        GlobalMaterialLocalizations.delegate,
        GlobalWidgetsLocalizations.delegate,
        GlobalCupertinoLocalizations.delegate,
      ],
      builder: (context, child) =>
          Directionality(textDirection: TextDirection.rtl, child: child!),
      home: ListenableBuilder(
        listenable: state,
        builder: (context, _) => state.profile == null
            ? OnboardingScreen(state: state)
            : HomeShell(state: state),
      ),
    );
  }
}
