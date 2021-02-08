from hcrystalball.wrappers._base import TSModelWrapper
from hcrystalball.wrappers._base import tsmodel_wrapper_constructor_factory

# redirect prophets and pystans output to the console
import logging
import sys
import itertools

sys_out = logging.StreamHandler(sys.__stdout__)
sys_out.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
logging.getLogger("fbprophet").addHandler(sys_out)
logging.getLogger("pystan").addHandler(sys_out)

from neuralprophet import NeuralProphet
import pandas as pd
from hcrystalball.utils import check_fit_before_predict
from hcrystalball.utils import check_X_y
from hcrystalball.utils import enforce_y_type
from hcrystalball.utils import deep_dict_update

pd.plotting.register_matplotlib_converters()


class NeuralProphetWrapper(TSModelWrapper):
    """Wrapper for fbprophet.Prophet model

    https://facebook.github.io/prophet/docs/quick_start.html#python-api

    Bring fbprophet to sklearn time-series compatible interface and puts fit parameters
    to initialization stage.

    Parameters
    ----------
    name : str
        Name of the model instance, used also as column name for returned prediction.

    full_prophet_output: bool
        Whether the `predict` method should output the full fbprophet.Prophet dataframe.

    extra_seasonalities : list of dicts
        Dictionary will be passed to fbprophet.Prophet add_regressor method.

    extra_regressors : list or list of dicts
        Dictionary will be passed to fbprophet.Prophet add_seasonality method.

    extra_holidays : dict of dict
        Dict with name of the holiday and values as another dict with required
        'lower_window' key and 'upper_window' key and optional 'prior_scale' key
        i.e.{'holiday_name': {'lower_window':1, 'upper_window:1, 'prior_scale: 10}}.

    fit_params : dict
        Parameters passed to `fit` fbprophet.Prophet model.

    clip_predictions_lower : float
        Minimal value allowed for predictions - predictions will be clipped to this value.

    clip_predictions_upper : float
        Maximum value allowed for predictions - predictions will be clipped to this value.
    """

    @tsmodel_wrapper_constructor_factory(NeuralProphet)
    def __init__(
        self,
        name="prophet",
        full_prophet_output=False,
        extra_seasonalities=None,
        extra_regressors=None,
        extra_holidays=None,
        fit_params={"freq": "D"},
        clip_predictions_lower=None,
        clip_predictions_upper=None,
    ):
        """This constructor will be modified at runtime to accept all
        parameters of the Prophet class on top of the ones defined here!"""
        pass

    def _transform_data_to_tsmodel_input_format(self, X, y=None, df_holidays=None):
        """Trasnform data into `Prophet.model` required format

        Parameters
        ----------
        X : pandas.DataFrame
            Input features with required 'date' column.

        y : array_like, (1d)
            Target vector

        Returns
        -------
        pandas.DataFrame
        """

        if y is not None:
            X = X.assign(y=y)
            self._df = X.assign(ds=lambda x: x.index).reset_index(drop=True)
            self._df = (
                self._df if df_holidays is None else self.model.create_df_with_events(self._df, df_holidays)
            )
            return self._df
        else:
            # if there is extra col in df and not in extra cols, will get ValueError
            hol_cols = self._get_holidays_cols(X)
            extra_reg = self._df[lambda x: x.columns.difference(["ds", "y"] + hol_cols)]
            extra_reg = None if extra_reg.empty else extra_reg
            hol_cols = self._get_holidays_cols(X)
            return self.model.make_future_dataframe(
                self._df, periods=len(X), events_df=df_holidays, regressors_df=extra_reg
            )

    def _set_model_extra_params(self, model):
        """Add `extra_seasonalities` and `extra_regressors` to `Prophet.model`

        Parameters
        ----------
        model : Prophet.model
            model to be extended with extra seasonalities and regressors

        Returns
        -------
        Prophet.model
            model with extra seasonalities and extra regressors
        """
        if self.extra_seasonalities is not None:
            for s in self.extra_seasonalities:
                model.add_seasonality(**s)
        if self.extra_regressors is not None:
            for r in self.extra_regressors:
                if isinstance(r, str):
                    model.add_future_regressor(r)
                else:
                    model.add_future_regressor(**r)
        return model

    def _get_holidays_cols(self, X):
        return [col for col in X.filter(like="_holiday_").select_dtypes(include="object").columns]

    def _adjust_holidays(self, X):
        """Add `holidays` to `Prophet.model`

        Doing that in required form and drop the 'holiday' column from X

        Parameters
        ----------
        X : pandas.DataFrame
            Input features with 'holiday' column.

        Returns
        -------
        pandas.DataFrame
            Input features without 'holiday' column
        """

        holiday_cols = self._get_holidays_cols(X)
        unique_holiday_dict = {col: X.loc[X[col] != "", col].unique() for col in holiday_cols}

        extra_holidays = {
            col: {
                holiday: {
                    "lower_window": self._get_holiday_windows(X, f"_before{col}"),
                    "upper_window": self._get_holiday_windows(X, f"_after{col}"),
                }
                for holiday in holidays
            }
            for col, holidays in unique_holiday_dict.items()
        }

        if self.extra_holidays:
            extra_holidays = {k: deep_dict_update(v, self.extra_holidays) for k, v in extra_holidays.items()}

        unique_holiday = set(itertools.chain.from_iterable(unique_holiday_dict.values()))
        all_extra_holidays = set(itertools.chain.from_iterable(extra_holidays.values()))
        if len(unique_holiday) > 0:
            missing_holidays = all_extra_holidays.difference(unique_holiday)

            if missing_holidays:
                logging.warning(
                    f"""Following holidays weren't found in data; thus not being
                        used {missing_holidays}. Available holidays for this data:
                        {unique_holiday}"""
                )

            holidays = []
            for col in holiday_cols:
                # assign country code/country code column to the holiday names
                # to ensure single occurence of a holiday per country
                # (e.g. `BE` and `DE` both have Christmas Day -> Christmas Day_DE, Christmas Day_BE)
                inter = X.loc[X[col] != "", [col]].assign(
                    **{"holiday": lambda df: df[col] + f"_{col.split('_')[2]}"}
                )
                if not inter.empty:
                    # translate original holiday name to extra information on the holiday affect
                    # given the extra_holidays parameter
                    holidays.append(
                        inter.merge(
                            inter[col].map(extra_holidays[col]).apply(pd.Series),
                            left_index=True,
                            right_index=True,
                        ).loc[
                            :,
                            ["holiday"],
                        ]
                    )

            df_holidays = (
                pd.concat(holidays)
                .assign(ds=lambda x: x.index)
                .reset_index(drop=True)
                .rename(columns={"holiday": "event"})
                .sort_values("event")
            )

        else:
            df_holidays = pd.DataFrame(columns=["ds", "event"])

        return df_holidays

    def _get_holiday_windows(self, X, col_like):
        """Get information about window for holidays for particular country.

        Parameters
        ----------
        X : pandas.DataFrame
            Input features with 'col_like' column.

        col_like: str
            col name pattern
            (i.e. `_before_holiday_DE`)
        Returns
        -------
        int
            number of days around holidays (whether before or after depends on `col_like`)
        """
        window = X.filter(like=f"{col_like}")
        window = 0 if window.empty else window.columns[0].split("_")[1]
        return int(window)

    @enforce_y_type
    @check_X_y
    def fit(self, X, y):
        """Transform input data to `Prophet.model` required format and fit the model.

        Parameters
        ----------
        X : pandas.DataFrame
            Input features.

        y : array_like, (1d)
            Target vector.

        Returns
        -------
        self
        """
        # TODO Add regressors which are not in self.extra_regressors but are in X?

        self.model = self._init_tsmodel(NeuralProphet)
        if X.filter(like="_holiday_").shape[1] > 0:
            df_holidays = self._adjust_holidays(X)
            X = X.drop(columns=self._get_holidays_cols(X), errors="ignore")
            self.model.add_events(df_holidays["event"].unique().tolist())
            df = self._transform_data_to_tsmodel_input_format(X, y, df_holidays)
        else:
            df = self._transform_data_to_tsmodel_input_format(X, y)
        self.model.fit(df, **self.fit_params) if self.fit_params else self.model.fit(df)
        self.fitted = True
        return self

    @check_fit_before_predict
    def predict(self, X):
        """Adjust holidays, transform data to required format and provide predictions.

        Parameters
        ----------
        X : pandas.DataFrame
            Input features.

        Returns
        -------
        pandas.DataFrame with pandas.DatetimeIndex
            Prediction is stored in column with name being the `name` of the wrapper.
            If `full_prophet_output` is set to True, then full Prophet.model.predict output is returned.
        """

        if X.filter(like="_holiday_").shape[1] > 0:
            df_holidays = self._adjust_holidays(X)
            X = X.drop(columns=self._get_holidays_cols(X), errors="ignore")
            df = self._transform_data_to_tsmodel_input_format(X, df_holidays=df_holidays)
        else:
            df = self._transform_data_to_tsmodel_input_format(X)

        preds = (
            self.model.predict(df)
            .rename(
                columns={
                    "yhat1": self.name,
                }
            )
            .drop(columns="ds", errors="ignore")
        )
        if not self.full_prophet_output:
            preds = preds[[self.name]]

        preds.index = X.index
        return self._clip_predictions(preds)


__all__ = ["NeuralProphetWrapper"]